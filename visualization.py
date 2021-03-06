import torch
from torch.autograd import Variable
from torch.autograd import Function
from torchvision import models
from torchvision import utils
import cv2
import os
import numpy as np
import argparse
import torch.nn as nn
import config as conf
from model.CNNs import FineTuneModel_Hierarchical
import sys
import shutil
import glob
from util.useful_imports import copyfile


class FeatureExtractor():
    """ Class for extracting activations and
    registering gradients from targetted intermediate layers """

    def __init__(self, model, target_layers):
        self.model = model
        self.target_layers = target_layers
        self.gradients = []

    def save_gradient(self, grad):
        self.gradients.append(grad)

    def __call__(self, x):
        outputs = []
        self.gradients = []
        for name, module in self.model._modules.items():
            x = module(x)
            if name in self.target_layers:
                # print('structure is ', module)
                x.register_hook(self.save_gradient)
                outputs += [x]
        return outputs, x


class ModelOutputs():
    """ Class for making a forward pass, and getting:
    1. The network output.
    2. Activations from intermeddiate targetted layers.
    3. Gradients from intermeddiate targetted layers. """

    def __init__(self, model, target_layers):
        self.model = model
        self.feature_extractor = FeatureExtractor(
            self.model.features, target_layers)

    def get_gradients(self):
        return self.feature_extractor.gradients

    def __call__(self, x):
        target_activations, output = self.feature_extractor(x)
        output = output.view(output.size(0), -1)
        output = self.model.classifier(output)
        return target_activations, output


def preprocess_image(img):
    means = [0.485, 0.456, 0.406]
    stds = [0.229, 0.224, 0.225]

    preprocessed_img = img.copy()[:, :, ::-1]
    for i in range(3):
        preprocessed_img[:, :, i] = preprocessed_img[:, :, i] - means[i]
        preprocessed_img[:, :, i] = preprocessed_img[:, :, i] / stds[i]
    preprocessed_img = \
        np.ascontiguousarray(np.transpose(preprocessed_img, (2, 0, 1)))
    preprocessed_img = torch.from_numpy(preprocessed_img)
    preprocessed_img.unsqueeze_(0)
    input = Variable(preprocessed_img, requires_grad=True)
    return input


def show_cam_on_image(img, mask, filename):
    heatmap = cv2.applyColorMap(np.uint8(255*mask), cv2.COLORMAP_JET)
    heatmap = np.float32(heatmap) / 255
    cam = heatmap + np.float32(img)
    cam = cam / np.max(cam)
    cv2.imwrite(filename, np.uint8(255 * cam))


class GradCam:
    def __init__(self, model, target_layer_names, use_cuda):
        self.model = model
        self.model.eval()
        self.cuda = use_cuda
        if self.cuda:
            self.model = model.cuda()

        self.extractor = ModelOutputs(self.model, target_layer_names)

    def forward(self, input):
        return self.model(input)

    def __call__(self, input, index=None):
        if self.cuda:
            features, output = self.extractor(input.cuda())
        else:
            features, output = self.extractor(input)

        if index is None:
            index = np.argmax(output.cpu().data.numpy())

        one_hot = np.zeros((1, output.size()[-1]), dtype=np.float32)
        one_hot[0][index] = 1
        one_hot = Variable(torch.from_numpy(one_hot), requires_grad=True)
        if self.cuda:
            one_hot = torch.sum(one_hot.cuda() * output)
        else:
            one_hot = torch.sum(one_hot * output)

        self.model.features.zero_grad()
        self.model.classifier.zero_grad()
        one_hot.backward(retain_graph=True)

        grads_val = self.extractor.get_gradients()[-1].cpu().data.numpy()

        target = features[-1]
        target = target.cpu().data.numpy()[0, :]

        weights = np.mean(grads_val, axis=(2, 3))[0, :]
        cam = np.zeros(target.shape[1:], dtype=np.float32)

        for i, w in enumerate(weights):
            cam += w * target[i, :, :]

        cam = np.maximum(cam, 0)
        cam = cv2.resize(cam, (224, 224))
        cam = cam - np.min(cam)
        cam = cam / np.max(cam)
        return cam


class GuidedBackpropReLU(Function):

    def forward(self, input):
        positive_mask = (input > 0).type_as(input)
        output = torch.addcmul(torch.zeros(
            input.size()).type_as(input), input, positive_mask)
        self.save_for_backward(input, output)
        return output

    def backward(self, grad_output):
        input, output = self.saved_tensors
        grad_input = None

        positive_mask_1 = (input > 0).type_as(grad_output)
        positive_mask_2 = (grad_output > 0).type_as(grad_output)
        grad_input = torch.addcmul(torch.zeros(input.size()).type_as(input), torch.addcmul(
            torch.zeros(input.size()).type_as(input), grad_output, positive_mask_1), positive_mask_2)

        return grad_input


class GuidedBackpropReLUModel:
    def __init__(self, model, use_cuda):
        self.model = model
        self.model.eval()
        self.cuda = use_cuda
        if self.cuda:
            self.model = model.cuda()

        # replace ReLU with GuidedBackpropReLU
        for idx, module in self.model.features._modules.items():
            if module.__class__.__name__ == 'ReLU':
                self.model.features._modules[idx] = GuidedBackpropReLU()

    def forward(self, input):
        return self.model(input)

    def __call__(self, input, index=None):
        if self.cuda:
            output = self.forward(input.cuda())
        else:
            output = self.forward(input)

        if index is None:
            index = np.argmax(output.cpu().data.numpy())

        one_hot = np.zeros((1, output.size()[-1]), dtype=np.float32)
        one_hot[0][index] = 1
        one_hot = Variable(torch.from_numpy(one_hot), requires_grad=True)
        if self.cuda:
            one_hot = torch.sum(one_hot.cuda() * output)
        else:
            one_hot = torch.sum(one_hot * output)

        # self.model.features.zero_grad()
        # self.model.classifier.zero_grad()
        one_hot.backward(retain_graph=True)

        output = input.grad.cpu().data.numpy()
        output = output[0, :, :, :]

        return output


def get_args():
    parser = argparse.ArgumentParser()
    parser.add_argument('--image-path', type=str, default='./examples/both.png',
                        help='Input image path')
    parser.add_argument('-g', type=str, default=1, help='Group')
    args = parser.parse_args()
    args.use_cuda = torch.cuda.is_available()
    if args.use_cuda:
        print("Using GPU for acceleration")
    else:
        print("Using CPU for computation")

    return args


class MyCNNs(nn.Module):
    def __init__(self, group=1):
        super(MyCNNs, self).__init__()
        model = self.get_model()
        self.features = model.features
        self.classifier = model.level_1_0 if group == 1 else model.level_1_1

    def forward(self, x):
        f = self.features(x)
        f = f.view(f.size(0), -1)
        y = self.classifier(f)
        return y

    def get_model(self):
        model = models.resnet18(pretrained=True)
        model = FineTuneModel_Hierarchical(model, 'resnet18', None)

        # load the best model
        checkpoint = torch.load(
            './output/best_resnet18.pth.tar',
            map_location=lambda storage, loc: storage
        )
        model.load_state_dict(checkpoint['state_dict'])
        model.args = checkpoint['args']
        model.classifier = model.level_1_1

        use_gpu = torch.cuda.is_available()
        if use_gpu:
            model = model.cuda()

        model.eval()
        return model


def make_sample(sample_dir):
    # create a  directory
    if os.path.exists(sample_dir):
        shutil.rmtree(sample_dir)
    os.mkdir(sample_dir)

    for label in os.listdir(conf.TRAIN_DIR):

        tar_dir = os.path.join(conf.TRAIN_DIR, label)
        img_list = glob.glob(tar_dir + '/*.jpg')

        if len(img_list) > 0:
            selected = np.random.randint(len(img_list))
            copyfile(img_list[selected], os.path.join(
                sample_dir, label + '.jpg'))


if __name__ == '__main__':
    """ python grad_cam.py <path_to_image>
    1. Loads an image with opencv.
    2. Preprocesses it for VGG19 and converts to a pytorch variable.
    3. Makes a forward pass to find the category index with the highest score,
    and computes intermediate activations.
    Makes the visualization. """
    sample_dir = './output/sample/'
    args = get_args()
    make_sample(sample_dir)

    # labels of group 1
    gr_0_lab = ['Kleine zandspiering', 'Smelt', 'Noordse zandspiering']
    gr_1_lab = ['Haring', 'Sprot', 'Fint']  # labels of group 2

    for layer in range(8):
        out_dir = os.path.join(sample_dir, str(layer + 1))
        if os.path.exists(out_dir):
            shutil.rmtree(out_dir)
        os.mkdir(out_dir)

    for label in gr_0_lab + gr_1_lab:
        args.g = int(label in gr_1_lab) + 1
        args.image_path = os.path.join(sample_dir, label + '.jpg')
        for layer in range(8):
            out_dir = os.path.join(sample_dir, str(layer + 1))

            grad_cam = GradCam(
                model=MyCNNs(group=args.g),
                target_layer_names=[str(layer)],
                use_cuda=args.use_cuda
            )

            img = cv2.imread(args.image_path, 1)
            if img is None:
                continue
            img = cv2.resize(img, (224, 224))
            cv2.imwrite(os.path.join(out_dir, label + '.jpg'), img)
            img = np.float32(img) / 255
            input = preprocess_image(img)

            # If None, returns the map for the highest scoring category.
            # Otherwise, targets the requested index.
            target_index = None

            mask = grad_cam(input, target_index)

            show_cam_on_image(img, mask, os.path.join(
                out_dir, label + '_cam.jpg'))
            """
            gb_model = GuidedBackpropReLUModel(
                model=MyCNNs(group=args.g), use_cuda=args.use_cuda)
            gb = gb_model(input, index=target_index)
            utils.save_image(torch.from_numpy(gb), os.path.join(
                sample_dir, label + '_gb.jpg'))

            cam_mask = np.zeros(gb.shape)
            for i in range(0, gb.shape[0]):
                cam_mask[i, :, :] = mask

            cam_gb = np.multiply(cam_mask, gb)
            utils.save_image(torch.from_numpy(cam_gb), os.path.join(
                sample_dir, label + '_cam_gb.jpg'))
            """
