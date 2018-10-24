import copy
import pickle

import torch
import torch.nn as nn
import torch.optim as optim
from torchvision.utils import save_image
import numpy as np

from Evaluation.Reviewer import *
from Generative_Models.discriminator import Discriminator, Discriminator_Cifar
from Generative_Models.generator import Generator, Generator_Cifar
from log_utils import save_images
from utils import variable
from copy import deepcopy

from Classifiers.Cifar_Classifier import Cifar_Classifier

class GenerativeModel(object):
    def __init__(self, args):

        self.args = args

        # parameters
        self.epoch = args.epoch_G
        self.sample_num = 100
        self.batch_size = args.batch_size
        self.dataset = args.dataset
        self.gpu_mode = args.gpu_mode
        self.model_name = args.gan_type
        self.conditional = args.conditional
        self.seed = args.seed
        self.generators = []
        self.c_criterion = nn.NLLLoss()
        self.size_epoch = args.size_epoch
        self.BCELoss = nn.BCELoss()
        self.device = args.device
        self.verbose = args.verbose

        self.save_dir = args.save_dir
        self.result_dir = args.result_dir
        self.data_dir = args.data_dir
        self.log_dir = args.log_dir
        self.gen_dir = args.gen_dir
        self.sample_dir = args.sample_dir

        self.task_type = args.task_type
        self.num_task = args.num_task
        self.num_classes = args.num_classes

        if self.dataset == 'mnist':
            self.z_dim = 62
            self.input_size = 1
            self.size = 28
        elif self.dataset == 'fashion':
            self.z_dim = 62
            self.input_size = 1
            self.size = 28
        elif self.dataset == 'cifar10':
            self.z_dim = 100
            self.input_size = 3
            self.size = 32

        if self.verbose:
            print("create G and D")

        if self.dataset=='cifar10':
            self.G = Generator_Cifar(self.z_dim, self.conditional)
            self.D = Discriminator_Cifar(self.conditional)
        else:
            self.G = Generator(self.z_dim, self.dataset, self.conditional, self.model_name)
            self.D = Discriminator(self.dataset, self.conditional, self.model_name)

        if self.verbose:
            print("create G and D 's optimizers")
        self.G_optimizer = optim.Adam(self.G.parameters(), lr=args.lrG, betas=(args.beta1, args.beta2))
        self.D_optimizer = optim.Adam(self.D.parameters(), lr=args.lrD, betas=(args.beta1, args.beta2))

        if self.gpu_mode:
            self.G=self.G.cuda(self.device)
            self.D=self.D.cuda(self.device)

        if self.verbose:
            print('---------- Networks architecture -------------')
            utils.print_network(self.G)
            utils.print_network(self.D)
            print('-----------------------------------------------')

        # fixed noise
        #self.sample_z_ = variable(torch.rand((self.sample_num, self.z_dim, 1, 1)), volatile=True)
        self.sample_z_ = variable(self.random_tensor(self.sample_num, self.z_dim))

        if self.dataset == 'mnist':
            self.Classifier = Mnist_Classifier(self.args)
        elif self.dataset == 'fashion':
            self.Classifier = Fashion_Classifier(self.args)
        elif self.dataset == 'cifar10':
            self.Classifier = Cifar_Classifier(self.args)

        if self.gpu_mode:
            self.Classifier.net = self.Classifier.net.cuda(self.device)

        # Logs
        self.train_hist = {}
        self.train_hist['D_loss'] = []
        self.train_hist['G_loss'] = []
        self.train_hist['per_epoch_time'] = []
        self.train_hist['total_time'] = []


    def test(self, predict, labels):
        correct = 0
        pred = predict.data.max(1)[1]
        correct = pred.eq(labels.data).cpu().sum()
        return correct, len(labels.data)

    def random_tensor(self, batch_size, z_dim):
        # Uniform distribution
        return torch.rand((batch_size, z_dim, 1, 1))

    # produce sample from one generator for visual inspection of a generator during training
    def visualize_results(self, epoch, classe=None, fix=True):

        sample_size=100

        # index allows, if there 5 task, to plot 2 classes for first task
        index = int(self.num_classes / self.num_task) * (classe + 1)

        self.G.eval()
        dir_path = self.result_dir
        if classe is not None:
            dir_path = self.result_dir + '/classe-' + str(classe)

        if not os.path.exists(dir_path):
            os.makedirs(dir_path)

        image_frame_dim = int(np.floor(np.sqrt(self.sample_num)))
        if self.conditional:


            y = torch.LongTensor(range(self.sample_num)) % self.num_classes
            y=y.view(self.sample_num, 1)

            y_onehot = torch.FloatTensor(self.sample_num, self.num_classes)
            y_onehot.zero_()
            y_onehot.scatter_(1, y, 1.0)
            y_onehot = variable(y_onehot)
        else:
            y_onehot = None

        
        if fix:
            """ fixed noise """
            if self.conditional:
                samples = self.G(self.sample_z_, y_onehot)
            else:
                samples = self.G(self.sample_z_)
        else:
            """ random noise """
            sample_z_ = variable(self.random_tensor(self.sample_num, self.z_dim), volatile=True)

            if self.conditional:
                samples = self.G(sample_z_, y_onehot)
            else:
                samples = self.G(self.sample_z_)

        if self.input_size == 1:
            if self.gpu_mode:
                samples = samples.cpu().data.numpy()
            else:
                samples = samples.data.numpy()
            samples = samples.transpose(0, 2, 3, 1)
            save_images(samples[:image_frame_dim * image_frame_dim, :, :, :], [image_frame_dim, image_frame_dim],
                        dir_path + '/' + self.model_name + '_epoch%03d' % epoch + '.png')
        else:
            save_image(samples[:self.sample_num].data, dir_path + '/' + self.model_name + '_epoch%03d' % epoch + '.png',
                       padding=0)

    # produce sample from all classes and return a batch of images and label
    # if no ind_task are given we generate all labellize for all task
    # if ind_task and multiplue_amnnotation == false we generate only for the actual task
    # if ind_task and multiplue_amnnotation == true we generate only for all past tasks
    def sample(self, batch_size=100, ind_task=None, multiple_annotation=False):

        self.G.eval()
        y = torch.ones(batch_size, 1)
        if self.conditional:

            z_ = self.random_tensor(batch_size, self.z_dim)
            if ind_task is not None:
                #y = torch.ones(batch_size, 1) * ind_task
                if multiple_annotation:
                    y = (torch.randperm(2*batch_size) % (ind_task+1))[:batch_size]
                    y = y.view(batch_size, 1).long()
                else:
                    y = torch.ones(batch_size, 1) * ind_task
                y = y.long()
            else:
                # keep this please
                #y = torch.LongTensor(batch_size, 1).random_() % self.num_classes
                y = (torch.randperm(2 * batch_size) % (self.num_classes + 1))[:batch_size]
                y = y.view(batch_size, 1)
            y_onehot = torch.FloatTensor(batch_size, self.num_classes)
            y_onehot.zero_().long()
            y_onehot.scatter_(1, y, 1.0)


            y_onehot = variable(y_onehot)
            output = self.G(variable(z_), y_onehot).data
        else:
            z_ = self.random_tensor(batch_size, self.z_dim)
            y = (torch.randperm(1000) % 10)[:batch_size]
            output = self.G(variable(z_))

            if multiple_annotation or ind_task is None:
                expert = copy.deepcopy(self.Classifier)
                expert.load_expert()
                expert.net.eval()
                if  ind_task is None:
                    y = expert.labelize(output, self.num_classes)
                else:
                    y = expert.labelize(output, ind_task)
                y = y.data
            else: # if no annotation we just give the ind_task as label (we should be here only for baseline)
                y = torch.ones(batch_size, 1) * ind_task
                y = y.long()
            output = output.data

        return output, y

    # load a conditonal generator, encoders and discriminators
    def load_G(self, ind_task):
        self.G.load_state_dict(
            torch.load(os.path.join(self.save_dir, self.model_name + '-' + str(ind_task) + '_G.pkl')))

    # save a generator in a given class
    def save_G(self, task):
        if not os.path.exists(self.save_dir):
            os.makedirs(self.save_dir)
        torch.save(self.G.state_dict(), os.path.join(self.save_dir, self.model_name + '-' + str(task) + '_G.pkl'))

    # save a generator, encoder and discriminator in a given class
    def save(self):
        if not os.path.exists(self.save_dir):
            os.makedirs(self.save_dir)

        torch.save(self.G.state_dict(), os.path.join(self.save_dir, self.model_name + '_G.pkl'))
        torch.save(self.D.state_dict(), os.path.join(self.save_dir, self.model_name + '_D.pkl'))

        with open(os.path.join(self.save_dir, self.model_name + '_history.pkl'), 'wb') as f:
            pickle.dump(self.train_hist, f)

    def train(self):
        self.G.train()
        self.D.train()

    def eval(self):
        self.G.eval()
        self.D.eval()

    def generate_task(self, ind_task, nb_sample_train, annotate=False):

        c1 = 0
        c2 = 1

        if self.dataset=='cifar10':
            nb_at_once = 100
        else:
            nb_at_once = 1000

        size_tensor=self.input_size*self.size*self.size

        if nb_sample_train >= nb_at_once:
            for i in range(int(nb_sample_train / nb_at_once)):
                tasks_tr = [] # reset the list
                x_tr, y_tr = self.sample(nb_at_once, ind_task, annotate)
                if self.gpu_mode:
                    x_tr, y_tr = x_tr.cpu(), y_tr.cpu()
                tasks_tr.append([(c1, c2), x_tr.clone().view(-1, size_tensor), y_tr.clone().view(-1)])
                if i == 0:
                    data_loader = DataLoader(tasks_tr, self.args)
                else:
                    data_loader.concatenate(DataLoader(tasks_tr, self.args))

            # here we generate the remaining samples
            if nb_sample_train % nb_at_once != 0:
                tasks_tr = [] # reset the list
                x_tr, y_tr = self.sample(nb_sample_train % nb_at_once, ind_task, annotate)
                if self.gpu_mode:
                    x_tr, y_tr = x_tr.cpu(), y_tr.cpu()
                tasks_tr.append([(c1, c2), x_tr.clone().view(-1, size_tensor), y_tr.clone().view(-1)])
                data_loader.concatenate(DataLoader(tasks_tr, self.args))

        else:
            tasks_tr = []
            x_tr, y_tr = self.sample(nb_sample_train, ind_task, annotate)
            if self.gpu_mode:
                x_tr, y_tr = x_tr.cpu(), y_tr.cpu()
            tasks_tr.append([(c1, c2), x_tr.clone().view(-1, size_tensor), y_tr.clone().view(-1)])
            data_loader = DataLoader(tasks_tr, self.args)

        return data_loader

    # This function generate a dataset for one class or for all class until ind_task included
    def generate_dataset(self, ind_task, nb_sample_train, one_task=True, Train=True):

        if Train:
            path = os.path.join(self.gen_dir, 'train_Task_' + str(ind_task) + '.pt')
            path_samples = os.path.join(self.sample_dir, 'samples_train_' + str(ind_task) + '.png')
        else:
            path = os.path.join(self.gen_dir, 'test_Task_' + str(ind_task) + '.pt')
            path_samples = os.path.join(self.sample_dir, 'samples_test_' + str(ind_task) + '.png')

        if self.num_task==5:

            for i in range(2*(ind_task + 1)):  # we take from all task, actual one included
                train_loader_ind = self.generate_task(i, nb_sample_train, annotate=True)

                if i == 0:
                    train_loader_gen = deepcopy(train_loader_ind)
                else:
                    train_loader_gen.concatenate(train_loader_ind)
        
        else:

            # if we have only on task to generate
            if one_task or ind_task == 0:  # generate only for the task ind_task
                train_loader_gen = self.generate_task(ind_task, nb_sample_train, annotate=True)

            else:  # else case we generate for all previous task
                for i in range(ind_task + 1):  # we take from all task, actual one included
                    train_loader_ind = self.generate_task(i, nb_sample_train, annotate=True)

                    if i == 0:
                        train_loader_gen = deepcopy(train_loader_ind)
                    else:
                        train_loader_gen.concatenate(train_loader_ind)

        # we save the concatenation of all generated with the actual task for train and test
        train_loader_gen.save(path)
        train_loader_gen.visualize_sample(path_samples, self.sample_num, [self.size, self.size, self.input_size])

        # return the the train loader with all data
        return train_loader_gen #test_loader_gen # for instance we don't use the test set

    # this generation only works for Baseline, disjoint
    # we generate the dataset based on one generator by task to get normally the best generated dataset
    # can be used to generate train or test data
    def generate_best_dataset(self, ind_task, nb_sample_train, Train=True):

        if Train:
            path = os.path.join(self.gen_dir, 'Best_train_Task_' + str(ind_task) + '.pt')
        else:
            path = os.path.join(self.gen_dir, 'Best_test_Task_' + str(ind_task) + '.pt')

        # if we have only on task to generate
        if ind_task == 0:  # generate only for the task ind_task
            # we do not need automatic annotation since we have one generator by class
            previous_data_train = self.generate_task(ind_task, nb_sample_train, annotate=False)
            #previous_data_train = DataLoader(tasks_tr, self.args)

        else:  # else we load the previous dataset and add the new data

            previous_path_train = os.path.join(self.gen_dir, 'Best_train_Task_' + str(ind_task - 1) + '.pt')

            previous_data_train = DataLoader(torch.load(previous_path_train), self.args)

            # we do not need automatic annotation since we have one generator by class
            train_loader_ind = self.generate_task(ind_task, nb_sample_train, annotate=False)

            previous_data_train.concatenate(train_loader_ind)

        # we save the concatenation of all generated with the actual task for train and test
        previous_data_train.save(path)

        # return nothing

    def get_one_hot(self, y):
        y_onehot = torch.FloatTensor(y.shape[0], self.num_classes)
        y_onehot.zero_()
        y_onehot.scatter_(1, y[:, np.newaxis], 1.0)

        return y_onehot
