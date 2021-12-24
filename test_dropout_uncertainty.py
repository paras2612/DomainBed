import numpy as np
import torch
import random
from torchvision import datasets
from torch import nn, optim, autograd
import torch.nn.functional as F
from domainbed import networks
import os


GLOBAL_DROPOUT_RATE = 0.5

class MNIST_DROPOUT_CNN(nn.Module):
    """
    Hand-tuned architecture for MNIST.
    Weirdness I've noticed so far with this architecture:
    - adding a linear layer after the mean-pool in features hurts
        RotatedMNIST-100 generalization severely.
    """
    n_outputs = 128

    def __init__(self, input_shape):
        super(MNIST_DROPOUT_CNN, self).__init__()

        self.conv1 = nn.Conv2d(input_shape[0], 64, 3, 1, padding=1)
        self.conv2 = nn.Conv2d(64, 128, 3, stride=2, padding=1)
        self.conv3 = nn.Conv2d(128, 128, 3, 1, padding=1)
        self.conv4 = nn.Conv2d(128, 128, 3, 1, padding=1)

        self.bn0 = nn.GroupNorm(8, 64)
        self.bn1 = nn.GroupNorm(8, 128)
        self.bn2 = nn.GroupNorm(8, 128)
        self.bn3 = nn.GroupNorm(8, 128)

        self.dr1 = nn.Dropout(GLOBAL_DROPOUT_RATE)
        self.dr2 = nn.Dropout(GLOBAL_DROPOUT_RATE)
        self.dr3 = nn.Dropout(GLOBAL_DROPOUT_RATE)
        self.dr4 = nn.Dropout(GLOBAL_DROPOUT_RATE)

        self.avgpool = nn.AdaptiveAvgPool2d((1, 1))

    def forward(self, x):
        x = self.conv1(x)
        x = F.relu(x)
        x = self.bn0(x)
        x = self.dr1(x)

        x = self.conv2(x)
        x = F.relu(x)
        x = self.bn1(x)
        x = self.dr2(x)

        x = self.conv3(x)
        x = F.relu(x)
        x = self.bn2(x)
        x = self.dr3(x)

        x = self.conv4(x)
        x = F.relu(x)
        x = self.bn3(x)
        x = self.dr4(x)

        x = self.avgpool(x)
        x = x.view(len(x), -1)
        return x


def DropoutClassifier(in_features, out_features, is_nonlinear=False):
    if is_nonlinear:
        return torch.nn.Sequential(
            torch.nn.Linear(in_features, in_features // 2),
            torch.nn.Dropout(GLOBAL_DROPOUT_RATE),
            torch.nn.ReLU(),
            torch.nn.Linear(in_features // 2, in_features // 4),
            torch.nn.Dropout(GLOBAL_DROPOUT_RATE),
            torch.nn.ReLU(),
            torch.nn.Linear(in_features // 4, out_features))
    else:
        return torch.nn.Linear(in_features, out_features)




mnist = datasets.MNIST('~/datasets/mnist', train=True, download=True)
mnist_train = (mnist.data[:50000], mnist.targets[:50000])
mnist_val = (mnist.data[50000:], mnist.targets[50000:])

rng_state = np.random.get_state()
np.random.shuffle(mnist_train[0].numpy())
np.random.set_state(rng_state)
np.random.shuffle(mnist_train[1].numpy())

# Build environments

device = 'cuda' if torch.cuda.is_available() else 'cpu'
def make_environment(images, labels, e):
    def torch_bernoulli(p, size):
        return (torch.rand(size) < p).float()
    def torch_xor(a, b):
        return (a-b).abs() # Assumes both inputs are either 0 or 1
    # 2x subsample for computational convenience
    images = images.reshape((-1, 28, 28))[:, ::2, ::2]
    # Assign a binary label based on the digit; flip label with probability 0.25
    labels = (labels < 5).float()
    gt_labels = labels
    labels = torch_xor(labels, torch_bernoulli(0.25, len(labels)))
    # Assign a color based on the label; flip the color with probability e
    colors = torch_xor(labels, torch_bernoulli(e, len(labels)))
    # Apply the color to the image by zeroing out the other color channel
    images = torch.stack([images, images], dim=1)
    images[torch.tensor(range(len(images))), (1-colors).long(), :, :] *= 0
    return {
        'images': (images.float() / 255.).to(device),
        'labels': labels[:, None].to(device),
        'gt_labels': gt_labels[:, None].to(device),
        'colors': colors.to(device)
    }

envs = [
make_environment(mnist_train[0][::2], mnist_train[1][::2], 0.2),
make_environment(mnist_train[0][1::2], mnist_train[1][1::2], 0.1),
make_environment(mnist_val[0], mnist_val[1], 0.9)
]
envs = [(e['images'], e['labels'], e['gt_labels'], e['colors']) for e in envs]

training_envs = envs[:-1]
test_envs = envs[-1:]
batch_size = 2048

def data_generator(dataset, batch_size=512):
    num_samples = len(dataset[0])
    indices = list(range(num_samples))
    while True:
        random.shuffle(indices)
        num_batches = int(num_samples/batch_size)
        for i in range(0, num_samples, batch_size):
            batch_indices = indices[i:i+batch_size]
            yield [dataset[0][batch_indices], dataset[1][batch_indices],
                   dataset[2][batch_indices], dataset[3][batch_indices]]


def flatten_reshape(x):
    flat_x = torch.flatten(x, start_dim=1)
    reshaped_x = flat_x.reshape(torch.cat([torch.tensor([1]), torch.tensor(flat_x.shape)]).tolist())
    return reshaped_x





def get_color_label_correlation(labels, colors):
    labels = labels.T[0]
    return np.correlate(labels, colors)/len(labels)


def mean_accuracy(logits, y):
    a = torch.argmax(logits, axis=1)
    b = torch.flatten(y)
    acc = torch.sum((a == b).type(torch.int))/len(a)
    return acc


def train_erm():
    num_epochs = 10000
    num_classes = 2
    input_shape = (2, 28, 28)
    hparams = {'data_augmentation': True,
     'resnet18': False,
     'resnet_dropout': 0.0,
     'class_balanced': False,
     'nonlinear_classifier': False,
     'lr': 0.005,
     'weight_decay': 0.0,
     'batch_size': 512}


    # Get the dropout based featurizer and classifier
    featurizer = MNIST_DROPOUT_CNN(input_shape)
    classifier = DropoutClassifier(featurizer.n_outputs, num_classes, hparams['nonlinear_classifier'])
    network = torch.nn.Sequential(featurizer, classifier)

    optimizer = torch.optim.Adam(network.parameters(), lr=hparams['lr'])

    # featurizer = networks.Featurizer(input_shape, hparams)
    # classifier = networks.Classifier(
    #     featurizer.n_outputs,
    #     num_classes,
    #     hparams['nonlinear_classifier']
    # )
    train_generators = [data_generator(x, batch_size=batch_size) for x in training_envs]
    test_generators = [data_generator(x) for x in test_envs]
    num_train_environments = len(train_generators)
    num_all_envs = num_train_environments + len(test_generators)

    num_uncertainty_predictions = 10

    # For each epoch
    for i in range(num_epochs):
        # Get the batch data from all the training environments
        env_x = []
        env_y = []
        for gen in train_generators:
            x, y, _, _ = next(gen)
            env_x.append(x)
            env_y.append(y)

        combined_x = torch.vstack(env_x)
        combined_y = torch.cat(env_y)

        # get multiple predictions by changing the dropouts(How many) (detach)
        prediction_list = []
        for i in range(num_uncertainty_predictions):
            prediction_list.append(network(combined_x))

        prediction_list = torch.stack(prediction_list)
        # find the variation between the sample predictions
            #
        # filter the samples which give the most stable predictions

        # Iterate on the most well defined(get the loss)
        loss = None
        optimizer.zero_grad()
        loss.backwards()
        optimizer.step()

        if i % 100 == 0:
            # Print the loss
            # Print the accuracy for the training environments
            # Print the accuracy for the test environments
            pass

    environment_networks = []


if __name__ == "__main__":
    # env_detector, env_predictor = train_environment_predictor()
    # print('====')
    train_erm()


