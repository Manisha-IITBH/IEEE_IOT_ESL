"""
Efficient Split Learning on CIFAR/FMNIST/DR/ISIC-2019 dataset.
"""

"""
IMPORTS
"""

# inbuilt
import os
import sys
import random
from math import ceil
import string
import requests, threading, time, socket, datetime
import multiprocessing
import copy
import importlib
import gc
from pathlib import Path

# usual
import numpy as np
import pandas as pd
import matplotlib.pyplot as plt
from scipy.interpolate import make_interp_spline
from tqdm.auto import tqdm
import wandb
from sklearn.metrics import balanced_accuracy_score, confusion_matrix
from torchmetrics.functional import f1_score


# torch
import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.optim import AdamW
from torch.optim.lr_scheduler import CosineAnnealingWarmRestarts

# PFSL
from utils.random_clients_generator import generate_random_clients
from utils.connections import send_object
from utils.argparser import parse_arguments
from utils.merge import merge_weights
from ImageClassification_Task.cifarbuilder import CIFAR10DataBuilder
from ImageClassification_Task.ic_client import Client
from ImageClassification_Task.ic_server import ConnectedClient

from config import WANDB_KEY

# flags
os.environ['CUDA_LAUNCH_BLOCKING']='1'



"""
ImageClassification IC CLASS
"""

class ICTrainer:

    def seed(self):
        """seed everything along with cuDNN"""
        seed = self.args.seed
        random.seed(seed)
        # torch.manual_seed(seed)
        # torch.cuda.manual_seed(seed)
        # torch.backends.cudnn.deterministic = True
        # torch.backends.cudnn.benchmark = False


    def init_clients_with_data(self,):
        """
        initialize PFSL clients: (id, class: Client)
        along with their individual data based on Flamby splits
        """
        assert self.args.number_of_clients <= 10, 'max clients for isic is 6'
        self.num_clients = self.args.number_of_clients if not self.pooling_mode else 1

        self.clients = generate_random_clients(self.num_clients,Client)

        if self.pooling_mode:
            key = list(self.clients.keys())[0]
            self.clients['pooled_client'] = self.clients.pop(key) 

        self.client_ids = list(self.clients.keys())


        for idx, (c_id, client) in enumerate(self.clients.items()):

            train_ds, test_ds, main_test_ds = self.cifar_builder.get_datasets(client_id=idx, pool=self.pooling_mode)

            client.train_dataset = train_ds
            client.test_dataset = test_ds
            client.main_test_dataset = main_test_ds

            print(f"client {c_id} -> #train {len(train_ds)} #valid: {len(test_ds)} #test: {len(main_test_ds)}")

            client.create_DataLoader(
                self.train_batch_size,
                self.test_batch_size
            )    
    
    def init_client_models_optims(self, input_channels=1):
        """
        Initialize client-side model splits: front, back, center.
        Splits are made from Resnet50/18, effnetv2_small,
        and optimizer for each split: Adam / AdamW / Novograd.
        """
        try:
            model = importlib.import_module(self.import_module)
        except ImportError as e:
            print(f"Error importing module: {e}")
            return
        
        pretrained = self.args.pretrained
        lr = self.args.client_lr

        for c_id, client in self.clients.items():
            client.device = self.device

            try:
                client.front_model = model.front().to(self.device)
                client.front_model.eval()
            except AttributeError as e:
                print(f"Error initializing front model for client {c_id}: {e}")
                continue

            try:
                client.back_model = model.back().to(self.device)
                client.back_optimizer = AdamW(client.back_model.parameters(), lr=lr, weight_decay=3e-2)
                #client.back_scheduler = CosineAnnealingWarmRestarts(
                #    client.back_optimizer,
                #    T_0=20,
                #    T_mult=1,
                #    eta_min=1e-10
                #)
            except AttributeError as e:
                print(f"Error initializing back model or optimizer for client {c_id}: {e}")
                continue

        print(f'Initialized client-side model splits front & back and their optimizers')
        
        
    def init_clients_server_copy(self):
        """
        For each client, there is a server copy of the center model
        initialized using class: ConnectedClient(client_id, connection).
        The center model and its optimizer are initialized as well.
        """
        try:
            model = importlib.import_module(self.import_module)
        except ImportError as e:
            print(f"Error importing module: {e}")
            return
        
        pretrained = self.args.pretrained
        lr = self.args.server_lr

        self.sc_clients = dict()
        for c_id in self.client_ids:
            self.sc_clients[c_id] = ConnectedClient(id=c_id, conn=None)

        for c_id, sc_client in self.sc_clients.items():
            sc_client.device = self.device

            try:
                sc_client.center_front_model = model.center_front().to(self.device)
                sc_client.center_front_model.eval()
            except AttributeError as e:
                print(f"Error initializing center front model for server copy of client {c_id}: {e}")
                continue

            try:
                sc_client.center_back_model = model.center_back().to(self.device)
                sc_client.center_optimizer = AdamW(sc_client.center_back_model.parameters(), lr=lr, weight_decay=3e-2)
                #sc_client.center_scheduler = CosineAnnealingWarmRestarts(
                #    sc_client.center_optimizer,
                #    T_0=20,
                #    T_mult=1,
                #    eta_min=1e-10
                #)
            except AttributeError as e:
                print(f"Error initializing center back model or optimizer for server copy of client {c_id}: {e}")
                continue

        print(f'Initialized server-side model splits center_front, center_back, and center_back optimizer')
    
    
    def _create_save_dir(self):
        """
        Create the directory for saving models. The directory is created based on
        the dataset name and model split specified in the arguments. If the directory
        already exists, it won't be recreated.

        Raises:
            OSError: If there is an error creating the directory.
        """
        try:
            self.save_dir = Path(f'./saved_models/{self.args.dataset}/key_value_mode/model_split{self.args.split}')
            self.save_dir.mkdir(exist_ok=True, parents=True)
            print(f"Directory created at {self.save_dir}")
        except OSError as e:
            print(f"Error creating directory {self.save_dir}: {e}")
            raise

    
    def remove_frozen_models(self,):
        """
        - the client-side forward model and the server-side center_front model are unused after
        the key-value store mappings are generated.
        - the models are rather saved to disk and moved to CPU during runtime to save GPU
        """
        for c_id in self.client_ids:
            # client-side front model
            front_state_dict = self.clients[c_id].front_model.state_dict()
            torch.save(front_state_dict, self.save_dir / f'client_{c_id}_front.pth')
            self.clients[c_id].front_model.cpu()
            # server-side center_front model
            center_front_state_dict = self.sc_clients[c_id].center_front_model.state_dict()
            torch.save(center_front_state_dict, self.save_dir / f'client_{c_id}_center_front.pth')
            self.sc_clients[c_id].center_front_model.cpu()



    def personalize(self,epoch):
        """
        personalization: 
            freeze all layers of center model in server copy clients
        """
        print(f'personalizing, freezing server copy center model @ epoch {epoch}')
        for c_id, sc_client in self.sc_clients.items():
            sc_client.center_back_model.freeze(epoch,pretrained=self.args.pretrained)
            
    def merge_model_weights(self, epoch):
        """
        Merge weights and distribute over all server-side center_back models.
        In the personalization phase, merging of weights of the back model layers is stopped:
            - Merge weights and distribute over all client-side back models if not in personalization mode.

        Args:
            epoch (int): The current epoch during which merging is applied.
        """
        print(f'Merging model weights at epoch {epoch}')

        params = []
        sample_lens = []

        # Collect the state dictionaries and sample lengths for all server-side center_back models
        for c_id, sc_client in self.sc_clients.items():
            try:
                params.append(copy.deepcopy(sc_client.center_back_model.state_dict()))
                sample_lens.append(len(self.clients[c_id].train_dataset) * self.args.kv_factor)
            except Exception as e:
                print(f"Error collecting weights for server copy client {c_id}: {e}")
                continue

        try:
            # Merge weights using a custom utility function
            w_glob = merge_weights(params, sample_lens)

            # Distribute the merged weights to all server-side center_back models
            for c_id, sc_client in self.sc_clients.items():
                sc_client.center_back_model.load_state_dict(w_glob)
                print(f"Merged weights loaded to server copy client {c_id}")
        except Exception as e:
            print(f"Error merging or distributing server-side weights: {e}")

        if not self.personalization_mode:
            params = []
            sample_lens = []

            # Collect the state dictionaries and sample lengths for all client-side back models
            for c_id, client in self.clients.items():
                try:
                    params.append(copy.deepcopy(client.back_model.state_dict()))
                    sample_lens.append(len(client.train_dataset))
                except Exception as e:
                    print(f"Error collecting weights for client {c_id}: {e}")
                    continue

            try:
                # Merge weights using a custom utility function
                w_glob_cb = merge_weights(params, sample_lens)
        
                # Distribute the merged weights to all client-side back models
                for c_id, client in self.clients.items():
                    client.back_model.load_state_dict(w_glob_cb)
                    print(f"Merged weights loaded to client {c_id}")
            except Exception as e:
                print(f"Error merging or distributing client-side weights: {e}")

        # Clean up to free memory
        del params, sample_lens

    
    def create_iters(self, dl='train'):
        """
        - Append 0 for train_f1/test_f1 per client list.
        - Assign iterators per client from train/test dataloader.
        """
        num_iters = {c_id: 0 for c_id in self.client_ids}
        for c_id, client in self.clients.items():
            if dl == 'train':
                client.train_f1.append(0)
                #client.iterator = iter(client.train_DataLoader)
                #client.num_iterations = len(client.train_DataLoader)
                #len_keys = len(self.sc_clients[c_id].activation_mappings)
                #num_iters[c_id] = int(ceil(len_keys / client.train_batch_size))
                #print(f"Client {c_id}: Added 0 to train_f1 list.")
                #print(f"Client {c_id}: Assigned train_DataLoader iterator.")
                #print(f"Client {c_id}: Number of train iterations set to {client.num_iterations}.")
                #print(f"Client {c_id}: len_keys for train Data: {len_keys}")
                #print(f"Client {c_id}: num_iters for train Data: {num_iters[c_id]}")
            elif dl == 'test':
                client.test_f1.append(0)
                #client.test_iterator = iter(client.test_DataLoader)
                #client.num_test_iterations = len(client.test_DataLoader)
                #num_iters[c_id] = len(client.test_DataLoader)
                #print(f"Client {c_id}: Added 0 to test_f1 list.")
                #print(f"Client {c_id}: Assigned test_DataLoader iterator.")
                #print(f"Client {c_id}: Number of test iterations set to {client.num_test_iterations}.")
                #print(f"Client {c_id}: len_keys for test Data: {len_keys}")
                #print(f"Client {c_id}: num_iters for test Data: {num_iters[c_id]}")

        print(f"Created iterators for {dl} data.")
        return num_iters
    
    
    def store_forward_mappings_kv(self,mode='train'):
        """
        kv: key-value store {data_key:np.Array}\n
        since client-side front model & server-side center-front model is "frozen"
        - we only need the outputs from both the models once
        - the outputs are stored in activation mappings, each output with its own index
        - the targets are stored in target mappings, each target with its own index
        
        these values are reused every epoch / refreshed once in a while, this is done for both train and test mappings
        """    
        if mode=='train':
            # create iterators for initial forward pass of training phase
            for c_id, client in self.clients.items():
                client.kv_flag=1
                self.sc_clients[c_id].kv_flag=1
                client.num_iterations = len(client.train_DataLoader)
                client.iterator = iter(client.train_DataLoader)
            # forward client-side front model which sets activation and target mappings.
            for c_id, client in tqdm(self.clients.items()):
                print(c_id,client.num_iterations)
                #for it in tqdm(range(client.num_iterations * self.args.kv_factor),desc="client front"):
                for it in tqdm(range(client.num_iterations * self.args.kv_factor),desc="client front"):
                    print(it)
                #    if client.data_key % len(client.train_dataset) == 0:
                    client.forward_front_key_value()
                    self.sc_clients[c_id].remote_activations1 = client.remote_activations1
                    self.sc_clients[c_id].batchkeys = client.key
                    self.sc_clients[c_id].forward_center_front()
                print("Train Dictionary Created")
                print("Dictionary keys Length:", len(list(self.sc_clients[c_id].activation_mappings.keys())))
                client.kv_flag=0
                self.sc_clients[c_id].kv_flag=0
        else:
            print("For Test dataset")
            # create iterators for initial forward pass of testing phase
            for c_id, client in self.clients.items():
                client.kv_test_flag=1
                self.sc_clients[c_id].kv_test_flag=1
                client.num_test_iterations = len(client.test_DataLoader)
                client.test_iterator = iter(client.test_DataLoader)
                
            # forward client-side front model which sets activation and target mappings.
            for c_id, client in tqdm(self.clients.items()):
                print(c_id,client.num_test_iterations)
                #for it in tqdm(range(client.num_iterations * self.args.kv_factor),desc="client front"):
                for it in tqdm(range(client.num_test_iterations)):
                    print(it)
                #    if client.data_key % len(client.train_dataset) == 0:
                    client.forward_front_key_value_test()
                    self.sc_clients[c_id].remote_activations1 = client.remote_activations1
                    self.sc_clients[c_id].test_batchkeys = client.test_key
                    self.sc_clients[c_id].forward_center_front_test()
                print("Test Dictionary keys Length:", list(self.sc_clients[c_id].test_activation_mappings.keys()))
                print("Test Dictionary Created")
                #print("Dictionary keys Length:", len(list(self.sc_clients[c_id].activation_mappings.keys())))
                client.kv_test_flag=0
                self.sc_clients[c_id].kv_test_flag=0

    def populate_key_value_store(self,):
        """
        - resets key-value store for client and server
        - populates key-value store kv_factor no. of times
        """

        #for c_id in self.client_ids:
        #    self.clients[c_id].data_key = 0
        #    self.clients[c_id].test_data_key = 0

        print('generating training samples in key-value store...')
        self.store_forward_mappings_kv(mode='train')
        print('generating testing samples in key-value store...')
        self.store_forward_mappings_kv(mode='test')


    def train_one_epoch(self,epoch):
        """
        in this epoch:
            - for every batch of data available:
                - forward center_back model of server
                - forward back model of client
                - step back optimizer
                - merge grads
                - step center_back optimizer
                - calculate batch metric

            - calculate epoch metric per client
            - calculate epoch metric avg. for all clients
            - merge model weights across clients (center_back & back)
        """

        print(f"training {epoch}..........................................................................................\n\n")
        num_iters = self.create_iters(dl='train')
        #max_iters = max(num_iters.values())
        self.overall_f1['train'].append(0)

        # set keys
        #for c_id, sc_client in self.sc_clients.items():
        #    sc_client.all_keys = list(sc_client.activation_mappings.keys())
        
        for client_id, client in tqdm(self.clients.items()):
                #client.train_acc.append(0)
                #client.loss_record.append(0)
                client.iterator = iter(client.train_DataLoader)
                #for it in tqdm(range(client.num_iterations * self.args.kv_factor),desc="client front"):
                for iteration in tqdm(range(client.num_iterations)):
                    client.forward_front_key_value()
                    #sc_clients[client_id].remote_activations1 = client.remote_activations1
                    self.sc_clients[client_id].batchkeys = client.key
                    self.sc_clients[client_id].forward_center_front()
                    self.sc_clients[client_id].forward_center_back()
                    client.remote_activations2 = self.sc_clients[client_id].remote_activations2
                    client.forward_back()
                    client.calculate_loss(mode='train')
                    wandb.log({'train step loss': client.loss.item()})
                    #client.calculate_flamby_loss()         #calculate_loss()
                    #client.loss_record[-1]+=client.calculate_flamby_loss()
                    client.loss.backward()
                    self.sc_clients[client_id].remote_activations2 = client.remote_activations2
                    self.sc_clients[client_id].backward_center()
                    client.step_back()
                    #client.back_scheduler.step()
                    client.zero_grad_back()
                    self.sc_clients[client_id].center_optimizer.step()
                    #self.sc_client[client_id].center_scheduler.step()
                    self.sc_clients[client_id].center_optimizer.zero_grad()
                    f1=client.calculate_train_metric()
                    client.train_f1[-1] += f1 
                    print("train f1 per iteration: ",iteration,f1)
                    wandb.log({f'train f1 / iter: client {client_id}':f1.item()})
                    #client.train_acc[-1] += client.calculate_train_flamby_acc()
        # calculate epoch metrics
        bal_accs, f1_macros = [], []
        avg_loss = 0
        for c_id, client in self.clients.items():
            #num_iters = len(client.activation_mappings.keys())
            client.train_f1[-1] /= client.num_iterations
            print("Train f1",client.train_f1)
            client.train_loss /= client.num_iterations
            print("train loss",client.train_loss)
            avg_loss += client.train_loss
            print("average loss",avg_loss)
            self.overall_f1['train'][-1] += client.train_f1[-1]
            print("average f1",self.overall_f1['train'][-1])
            #print("pred shape", client.train_preds)
            print("target shape",client.train_targets)
            bal_acc_client, f1_macro_client = client.get_main_metric(mode='train') 
            bal_accs.append(bal_acc_client)
            f1_macros.append(f1_macro_client)
            wandb.log({f'avg train f1 {c_id}': client.train_f1[-1].item()})
            wandb.log({f'balanced acc train {c_id}':bal_acc_client})
            wandb.log({f'f1 macro train {c_id}':f1_macro_client})
            wandb.log({f'avg train loss {c_id}': client.train_loss})
            client.train_loss = 0 # reset for next epoch


        # calculate epoch metrics across clients
        bal_acc = np.array(bal_accs).mean()
        f1_macro = np.array(f1_macros).mean()
        self.overall_f1['train'][-1] /= self.num_clients
        print("train f1: ", self.overall_f1['train'][-1])
        print("train acc: ", bal_acc)
        wandb.log({'avg train f1 all clients': self.overall_f1['train'][-1].item()})
        wandb.log({'avg train bal acc all clients': bal_acc})
        wandb.log({'avg train f1 macro all clients': f1_macro})
        wandb.log({'avg train loss all clients': avg_loss / self.num_clients})

        if not self.pooling_mode:
            # merge model weights (center and back)
            self.merge_model_weights(epoch)

    
    @torch.no_grad()
    def test_one_epoch(self,epoch):
        """
        in this epoch:
            - for every batch of data available:
                - forward center_back model of server
                - forward back model of client
                - calculate batch metric

            - calculate epoch metric per client
            - calculate epoch metric avg. for all clients
        """

        num_iters = self.create_iters(dl='test')
        #max_iters = max(num_iters.values())
        self.overall_f1['test'].append(0)

        for c_id, client in self.clients.items():
            client.pred = []
            client.y = []

        # set keys
        #for c_id, sc_client in self.sc_clients.items():
        #    sc_client.all_keys = list(sc_client.test_activation_mappings.keys())
        for client_id, client in tqdm(self.clients.items()):
                #client.train_acc.append(0)
                #client.loss_record.append(0)
                #client.iterator = iter(client.test_DataLoader)
                client.num_test_iterations = len(client.test_DataLoader)
                client.test_iterator = iter(client.test_DataLoader)
                #for it in tqdm(range(client.num_iterations * self.args.kv_factor),desc="client front"):
                for iteration in tqdm(range(client.num_test_iterations)):
                    client.forward_front_key_value_test()
                    #sc_clients[client_id].remote_activations1 = client.remote_activations1
                    self.sc_clients[client_id].test_batchkeys = client.test_key
                    self.sc_clients[client_id].forward_center_front_test()
                    self.sc_clients[client_id].forward_center_back()
                    client.remote_activations2 = self.sc_clients[client_id].remote_activations2
                    client.forward_back()
                    client.calculate_loss(mode='test')
                    print("loss calculated")
                    wandb.log({'train step loss': client.loss.item()})
                    #client.calculate_flamby_loss()         #calculate_loss()
                    #client.loss_record[-1]+=client.calculate_flamby_loss()
                    #client.loss.backward()
                    #self.sc_clients[client_id].remote_activations2 = client.remote_activations2
                    #self.sc_clients[client_id].backward_center()
                    #client.step_back()
                    #client.back_scheduler.step()
                    #client.zero_grad_back()
                    #self.sc_clients[client_id].center_optimizer.step()
                    #self.sc_client[client_id].center_scheduler.step()
                    #self.sc_clients[client_id].center_optimizer.zero_grad()
                    print("calculate loss metric")
                    f1=client.calculate_test_metric()
                    print("done")
                    client.test_f1[-1] += f1 
                    print("validation f1 per iteration: ",iteration,f1)
                    wandb.log({f'Validation f1 / iter: client {client_id}':f1.item()})
                    
                # calculate epoch metrics
        avg_loss = 0
        bal_accs,f1_macros = [], []
        for c_id, client in self.clients.items():
            client.test_f1[-1] /= len(client.test_DataLoader)
            client.test_loss /= len(client.test_DataLoader)

            # calculate remaining metrics
            avg_loss += client.test_loss
            bal_acc_client, f1_macro_client = client.get_main_metric(mode='test')
            bal_accs.append(bal_acc_client)
            f1_macros.append(f1_macro_client)
            self.overall_f1['test'][-1] += client.test_f1[-1]
            wandb.log({f'avg test f1 {c_id}': client.test_f1[-1].item()})
            wandb.log({f'balanced acc test {c_id}':bal_acc_client})
            wandb.log({f'f1 macro test {c_id}':f1_macro_client})
            wandb.log({f'avg test loss {c_id}': client.test_loss})
            client.test_loss = 0 # reset for next epoch

        # calculate epoch metrics across clients
        bal_acc = np.array(bal_accs).mean()
        f1_macro = np.array(f1_macros).mean()
        self.overall_f1['test'][-1] /= self.num_clients
        print("test f1: ", self.overall_f1['test'][-1])
        print("test acc: ", bal_acc)
        wandb.log({'avg test f1 all clients': self.overall_f1['test'][-1].item()})
        wandb.log({'avg test bal acc all clients': bal_acc})
        wandb.log({'avg test f1 macro all clients': bal_acc})
        wandb.log({'avg test loss all clients': avg_loss / self.num_clients})
        
        
        # max f1 score achieved on test dataset
        #if(bal_acc > self.max_acc):
        #    self.max_acc=bal_acc
        #    self.max_epoch=epoch
        #    print(f"MAX val acc score: {self.max_acc} @ epoch {self.max_epoch}")
        #    wandb.log({
        #        'max test f1 score':self.max_acc.item(),
        #        'max_test_f1_epoch':self.max_epoch
        #    })
        #    # save at best model
        #    return True
        
        #return False # don't save
        
        """# max f1 score achieved on test dataset
        if(self.overall_f1['test'][-1]> self.max_f1['f1']):
            self.max_f1['f1']=self.overall_f1['test'][-1]
            self.max_f1['epoch']=epoch
            print(f"MAX test f1 score: {self.max_f1['f1']} @ epoch {self.max_f1['epoch']}")
            wandb.log({
                'max test f1 score':self.max_f1['f1'].item(),
                'max_test_f1_epoch':self.max_f1['epoch']
            })
            # save at best model
            return True
        
        return False # don't save

                 # per iteration in testing epoch, do the following:
        for it in tqdm(range(max_iters)):

            # forward server-side center_back model with activations
            for c_id, sc_client in self.sc_clients.items():
                if num_iters[c_id] != 0:
                    sc_client.current_keys=list(np.random.choice(sc_client.all_keys, min(self.clients[c_id].test_batch_size, len(sc_client.all_keys)), replace=False))
                    sc_client.update_all_keys()
                    sc_client.middle_activations=torch.Tensor(np.array([sc_client.test_activation_mappings[x] for x in sc_client.current_keys])).to(self.device).detach().requires_grad_(True)

                    sc_client.forward_center_back()

            # forward client-side back model with activations
            for c_id, client in self.clients.items():
                if num_iters[c_id] != 0:
                    client.current_keys = self.sc_clients[c_id].current_keys
                    client.remote_activations2 = self.sc_clients[c_id].remote_activations2

                    client.forward_back()
                    client.set_test_targets()

            # calculate test loss
            for c_id, client in self.clients.items():
                if num_iters[c_id] != 0:
                    client.calculate_loss(mode='test')
                    wandb.log({'test step loss': client.loss.item()})

            # test f1 of every client in the current epoch in the current batch
            for c_id, client in self.clients.items():
                if num_iters[c_id] != 0:
                    f1=client.calculate_test_metric()
                    client.test_f1[-1] += f1 
                    print("test f1 per iteration: ", f1)
                    wandb.log({f'test f1 / iter: client {c_id}':f1.item()})

            # reduce num_iters per client by 1
            # testing loop will only execute for a client if iters are left 
            for c_id in self.client_ids:
                if num_iters[c_id] != 0:
                    num_iters[c_id] -= 1
            """
        
    def save_models(self,):
        """
        save client-side back and server-side center_back models to disk
        """
        for c_id in self.client_ids:
            # client-side front model
            front_state_dict = self.clients[c_id].front_model.state_dict()
            torch.save(front_state_dict, self.save_dir / f'client_{c_id}_{self.args.model}_front.pth')
            # server-side center_front model
            center_front_state_dict = self.sc_clients[c_id].center_front_model.state_dict()
            torch.save(center_front_state_dict, self.save_dir / f'client_{c_id}_{self.args.model}_center_front.pth')
            # server-side center_back model
            center_back_state_dict = self.sc_clients[c_id].center_back_model.state_dict()
            torch.save(center_back_state_dict, self.save_dir / f'client_{c_id}_{self.args.model}_center_back.pth')
            # client-side back model
            back_state_dict = self.clients[c_id].back_model.state_dict()
            torch.save(back_state_dict, self.save_dir / f'client_{c_id}_{self.args.model}_back.pth')

    def load_best_models(self,):
        """
        replaces the latest models with the best models on server and client-side
        """

        model = importlib.import_module(self.import_module)

        for c_id in self.client_ids:

            front = model.front().to(self.device)
            front_sd = torch.load(self.save_dir / f'client_{c_id}_{self.args.model}_front.pth')
            front.load_state_dict(front_sd)
            center_front = model.center_front().to(self.device)
            center_front_sd = torch.load(self.save_dir / f'client_{c_id}_{self.args.model}_center_front.pth')
            center_front.load_state_dict(center_front_sd)
            center_back = model.center_back().to(self.device)
            center_back_sd = torch.load(self.save_dir / f'client_{c_id}_{self.args.model}_center_back.pth')
            center_back.load_state_dict(center_back_sd)
            back = model.back().to(self.device)
            back_sd = torch.load(self.save_dir / f'client_{c_id}_{self.args.model}_back.pth')
            back.load_state_dict(back_sd)

            self.clients[c_id].front_model = front
            self.clients[c_id].back_model = back
            self.sc_clients[c_id].center_front_model = center_front
            self.sc_clients[c_id].center_back_model = center_back


    @torch.no_grad()
    def inference(self,):
        """
        run inference on the main test dataset
        """

        print("RUNNING INFERENCE from the best models on test dataset")

        #self.load_best_models()
        avg_acc=0
        for c_id in self.client_ids:

            trues = []
            preds = []

            for batch in self.clients[c_id].main_test_DataLoader:
                image, label = batch['image'].to(self.device), batch['label'].to(self.device)

                x1 = self.clients[c_id].front_model(image)
                x2 = self.sc_clients[c_id].center_front_model(x1)
                x3 = self.sc_clients[c_id].center_back_model(x2)
                x4 = self.clients[c_id].back_model(x3)

                trues.append(label.cpu())
                preds.append(x4.cpu())

            #preds = torch.vstack(preds)
            #targets = torch.vstack(trues)
            preds = torch.cat(preds)
            targets = torch.cat(trues)
            print("Shapes - preds:", preds.shape, "targets:", targets.shape)
            targets = targets.reshape(-1).numpy()
            preds = np.argmax(preds.numpy(), axis=1)
            correct = np.sum(preds == targets)
            total = len(targets)
            accuracy = correct / total
            #bacc = balanced_accuracy_score(targets, preds)
            wandb.log({
                'inference cfm': wandb.plot.confusion_matrix(
                    preds=preds,
                    y_true=targets,
                    class_names=[f'{i}' for i in range(10)]
                )
            })

            print(f'inference score {c_id}: {accuracy}')
            avg_acc+=accuracy
            wandb.log({f'inference score {c_id}': accuracy})
        print(f'Average inference score: {avg_acc/10}')


    def clear_cache(self,):
        gc.collect()
        torch.cuda.empty_cache()


    def fit(self,):
        """
        - trains and tests the models for given num. of epochs
        """

        if self.pooling_mode:
            print('\n\nPOOLING MODE: ENABLED!')

        self._create_save_dir()

        # disabled freeing GPU mem since key-value store needs to be refreshed
        # print('freeing some GPU...')
        # self.remove_frozen_models()

        # if key value store refresh rate = 0, it is disabled
        if self.args.kv_refresh_rate == 0:
            self.populate_key_value_store()
            self.clear_cache()
        
        
        print(f'{"-"*25}\n\ncommence training...\n\n')

        for epoch in tqdm(range(self.args.epochs)):

            # if key value store refresh rate != 0, it is enabled
            if self.args.kv_refresh_rate != 0:
                if epoch % self.kv_refresh_rate == 0:
                    print(f'\npreparing key value store for the next {self.kv_refresh_rate} epochs\n\n')
                    self.populate_key_value_store()
                    self.clear_cache()

            if self.args.personalize:
                if epoch == self.args.p_epoch:
                    self.personalization_mode = True
                    self.load_best_models()
                    self.personalize(epoch)

            wandb.log({'epoch':epoch})

            for c_id in self.client_ids:
                self.clients[c_id].back_model.train()
                self.sc_clients[c_id].center_back_model.train()

            self.train_one_epoch(epoch)
            self.clear_cache()

            for c_id in self.client_ids:
                self.clients[c_id].back_model.eval()
                self.sc_clients[c_id].center_back_model.eval()

            #is_best = self.test_one_epoch(epoch)
            self.test_one_epoch(epoch)
            #if is_best:
            #    self.save_models()

            self.clear_cache()
            self.inference()

        # final metrics
        print(f'\n\n\n{"::"*10}BEST METRICS{"::"*10}')
        print("Training Mean f1 Score: ", self.overall_f1['train'][self.max_f1['epoch']])
        print("Maximum Test Mean f1 Score: ", self.max_f1['f1'])



    def __init__(self,args):
        """
        implementation of PFSL training & testing simulation on ISIC-2019 dataset
            - ISIC-2019: Dermoscopy Image Classification
            - model: ResNet

        initialize everything:
            - data
            - wandb logging
            - metrics
            - device
            - batch sizes
            - flags
            - models
            - pooling mode: train all samples together
        """

        self.args = args
        self.log_wandb = self.args.wandb

        self.import_module = f"ImageClassification_Task.models.{self.args.model}_split{self.args.split}"

        self.pooling_mode = self.args.pool

        # refresh key-value store every N epochs
        self.kv_refresh_rate = self.args.kv_refresh_rate

        wandb.login(key=WANDB_KEY)
        self.run = wandb.init(
            project='med-fsl_isic2019',
            config=vars(self.args),
            job_type='train',
            mode='online' if self.log_wandb else 'disabled'
        )

        self.seed()

        #self.isic = ISICDataBuilder()
        self.cifar_builder = CIFAR10DataBuilder()

        self.device = 'cuda' if torch.cuda.is_available() else 'cpu'
        #self.device = torch.device("cuda:1" if torch.cuda.is_available() else "cpu")

        self.overall_f1 = {
            'train': [],
            'test': []
        }

        self.max_f1 = {
            'f1': 0,
            'epoch': -1
        }

        self.train_batch_size = self.args.batch_size
        self.test_batch_size = self.args.test_batch_size

        self.personalization_mode = False
        
        self.max_acc = 0
        self.max_epoch = 0

        self.init_clients_with_data()

        self.init_client_models_optims()

        self.init_clients_server_copy()



if __name__ == '__main__':
    args = parse_arguments()
    trainer = ISICTrainer(args)
    trainer.fit()
    trainer.inference()