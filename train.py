import os
import time
import json
import torch
import random
import parameters
import grammar_model
import numpy as np
from GVAE import GrammarVAE
from datetime import datetime
from built_in_network import FeedForward
from torch.utils.data import DataLoader, random_split
from torch.optim.lr_scheduler import ReduceLROnPlateau
from utils import AnnealKL, TrainQM9dataset, write_csv

from torchinfo import summary

seed = 42
torch.manual_seed(seed)
random.seed(seed)
np.random.seed(seed)

device = "cuda" if torch.cuda.is_available() else "cpu"
today = datetime.now()

def prop_run(params):
    
    saving_path = 'results/' + f'{params["prop"]}/' + today.strftime('%d_%m_%Y') + f'_{today.hour}_{today.minute}_{today.second}'
    
    epochs = params['epochs']             
    batch = params['batch']               
    max_length = params['max_length']  # it's the max number of rules needed to create a SMILES
    latent_dim = params['latent_dim']
    n_layers = params['n_layers']  # num of layers for GRU decoder
    hidden_layer = params['hidden_layer_prop']  # num of neurons of the property model
    min_valid_loss = np.inf

    # loading the data
    dataset = TrainQM9dataset(params['dataset_path'], params['labels_path'], params['normalization'])

    # splitting training and validation
    chunk = int(params['valid_split'] * len(dataset))  
    train_split, validation_split = random_split(dataset, [len(dataset) - chunk, chunk])

    trainloader = DataLoader(train_split, batch_size=batch, drop_last=True, shuffle=False, num_workers=2, pin_memory=True)
    validloader = DataLoader(validation_split, batch_size=batch, drop_last=True, shuffle=False, num_workers=2, pin_memory=True)
    
    # create model
    model = GrammarVAE()
    # print(summary(model, input_size=(params['batch'], 67, 100)))

    # load property prediction model
    pp_model = FeedForward(latent_dim, hidden_layer)
   # print(summary(pp_model, input_size=(batch, latent_dim)))
   
    if torch.cuda.is_available():
        model.cuda()
        pp_model.cuda()
        
    # optimizer and loss. the same optimizer will be used for both the VAE and the built-in feedforward neural network
    optimizer = torch.optim.Adam(list(model.parameters()) + list(pp_model.parameters()), lr=params['learning_rate'], amsgrad=True)
    scheduler = ReduceLROnPlateau(optimizer, mode='min', patience=5, min_lr=1e-6, verbose=True)
    criterion = torch.nn.BCELoss()
    pp_loss = torch.nn.MSELoss()
    
    # annealing the kl weight if needed
    if params['anneal_kl']:
        anneal = AnnealKL(n_epoch=epochs, n_cycle=params['n_cycle'], ratio=params['ratio_anneal_kl'])

    # dict to save the results
    log = {'elbo': [], 'kl':[], 'reconstruction':[], 'mse': []}
    log_val = {'elbo': [], 'kl':[], 'reconstruction':[], 'mse': []}
    
    # creating the folder to save the results
    os.makedirs(saving_path)
    
    # saving used params for each run
    with open(os.path.join(saving_path, 'params.json'), 'w') as file:
        json.dump(params, file)
        
    # weights for the loss
    kl_weight = params['kl_weight']
    recons_weight = params['reconstruction_weight']
    prop_weight = params['prop_weight']

    for epoch in range(epochs):

      if params['anneal_kl']:
        beta = anneal.beta(epoch)
      
      model.train() 
      pp_model.train()
    
      avg_elbo, avg_kl, avg_recons, avg_mse = 0, 0, 0, 0
      for x, label in trainloader:
    
        optimizer.zero_grad()
        # training procedure -----------------------------------------------------
        x = x.transpose(1, 2).contiguous().to(device)  # [batch, NUM_OF_RULES, MAX_LEN]
        z, mu, sigma, logits = model(x)

        predictions = pp_model(z)
        
        # returning x to its original dimensions
        x = x.transpose(1, 2).contiguous()  # [batch, MAX_LEN, NUM_OF_RULES]
        x_decoded_mean = model.conditional(x, logits) 
        
        # calculating the errors
        reconstruction_loss = max_length * criterion(x_decoded_mean.view(-1), x.view(-1)) 
        kl = model.kl(mu, sigma)
        
        property_loss = pp_loss(predictions.view(-1), label.to(device).float()) 
        
        # annealing weigth beta to the kl
        if params['anneal_kl']:
            elbo = recons_weight * reconstruction_loss + kl_weight * kl * beta + property_loss * prop_weight
            
        else:
            elbo = recons_weight * reconstruction_loss + kl_weight * kl + property_loss * prop_weight
        
        # update parameters
        elbo.backward()
        optimizer.step()
      
        # adding the error per batch
        avg_elbo += elbo.item()
        avg_kl += kl.item()
        avg_recons += reconstruction_loss.item()
        avg_mse += property_loss.item()
    
      # saving the results
      if params['anneal_kl']:
          log['kl'].append((avg_kl * beta * kl_weight)/len(trainloader))
      else:
          log['kl'].append((avg_kl * kl_weight)/len(trainloader))
          
      log['elbo'].append(avg_elbo/len(trainloader))
      log['reconstruction'].append((avg_recons * recons_weight)/len(trainloader))
      log['mse'].append((avg_mse * prop_weight)/len(trainloader))
      
      write_csv(log, os.path.join(saving_path, 'log.csv'))
      
################################################################################
      
      # validation procedure -----------------------------------------------------
      model.eval()
      pp_model.eval()
    
      avg_elbo_val, avg_kl_val, avg_recons_val, avg_mse_val = 0, 0, 0, 0
      
      with torch.no_grad():
          for x_val, label_val in validloader:
        
            x_val = x_val.transpose(1, 2).contiguous().to(device)  # [batch, 76, 100]
            z_val, mu_val, sigma_val, logits_val = model(x_val)

            predictions_val = pp_model(z_val)
            
            # returning x to its original dimensions
            x_val = x_val.transpose(1, 2).contiguous()  # [batch, 100, 76]
            x_decoded_mean_val = model.conditional(x_val, logits_val)  
        
            # calculating the errors
            reconstruction_loss_val = max_length * criterion(x_decoded_mean_val.view(-1), x_val.view(-1))
            kl_val = model.kl(mu_val, sigma_val) 
            
            property_loss_val = pp_loss(predictions_val.view(-1), label_val.to(device).float()) 
            
            if params['anneal_kl']:
                elbo_val = recons_weight * reconstruction_loss_val + kl_weight * kl_val * beta + property_loss_val * prop_weight
            else:
                elbo_val = recons_weight * reconstruction_loss_val + kl_weight * kl_val + property_loss_val * prop_weight
    
            # adding the error per batch
            avg_elbo_val += elbo_val.item()
            avg_kl_val += kl_val.item()
            avg_recons_val += reconstruction_loss_val.item()
            avg_mse_val += property_loss_val.item()
        
      print(f"epoch: {epoch+1}/{epochs}\nelbo: {avg_elbo/len(trainloader):>5f}  kl: {((avg_kl * beta * kl_weight)/len(trainloader) if params['anneal_kl'] else (avg_kl * kl_weight) /len(trainloader):>5f}  reconstruction: {(avg_recons * recons_weight)/len(trainloader):>5f}  mse: {(avg_mse * prop_weight)/len(trainloader):>5f} ----- elbo_val: {avg_elbo_val/len(validloader):>5f}  kl_val: {((avg_kl_val * beta * kl_weight)/len(validloader) if params['anneal_kl'] else (avg_kl_val * kl_weight)/len(validloader)):>5f}  reconstruction_val: {(avg_recons_val * recons_weight)/len(validloader):>5f}  mse_val: {(avg_mse_val * prop_weight)/len(validloader):>5f}")
      
      # saving the results
      if params['anneal_kl']:
        log_val['kl'].append((avg_kl_val * beta * kl_weight)/len(validloader))
      else:
        log_val['kl'].append((avg_kl_val * kl_weight)/len(validloader))
    
      log_val['elbo'].append(avg_elbo_val/len(validloader))
      log_val['reconstruction'].append((avg_recons_val * recons_weight)/len(validloader))
      log_val['mse'].append((avg_mse_val * prop_weight)/len(validloader))
    
      if min_valid_loss > avg_elbo_val:
        print(f'Validation loss decreased {min_valid_loss/len(validloader):.5f} ---> {avg_elbo_val/len(validloader):.5f}. Saving the model!')
        min_valid_loss = avg_elbo_val
        
        # saving the encoder and decoder separately and also the whole model
        torch.save(model.state_dict(), os.path.join(saving_path, 'gvae_model.pth'))
        # encoder
        torch.save(model.encoder.state_dict(), os.path.join(saving_path, 'gvae_encoder.pth'))
        # decoder
        torch.save(model.decoder.state_dict(), os.path.join(saving_path, 'gvae_decoder.pth'))
    
      write_csv(log_val, os.path.join(saving_path, 'log_val.csv'))
      
      scheduler.step(avg_elbo_val)
     
  
def no_prop_run(params):

    saving_path = 'results/no_prop/' + today.strftime('%d_%m_%Y') + f'_{today.hour}_{today.minute}_{today.second}'
    os.makedirs(saving_path)
    
    # saving the params for each run
    with open(os.path.join(saving_path, 'params.json'), 'w') as file:
        json.dump(params, file)
  
    epochs = params['epochs']
    batch = params['batch']
    max_length = params['max_length']
    latent_dim = params['latent_dim']
    n_layers = params['n_layers']  # num of layers for GRU decoder
    hidden_layer = params['hidden_layer_prop']  # num of neurons of the property model
    min_valid_loss = np.inf

    # loading the data
    dataset = TrainQM9dataset(params['dataset_path'], params['labels_path'], params['normalization'])

    # splitting training and validation
    chunk = int(params['valid_split'] * len(dataset))  
    train_split, validation_plit = random_split(dataset, [len(dataset) - chunk, chunk])

    trainloader = DataLoader(train_split, batch_size=batch, drop_last=True, shuffle=False, num_workers=2, pin_memory=True)
    validloader = DataLoader(validation_plit, batch_size=batch, drop_last=True, shuffle=False, num_workers=2, pin_memory=True)

    # create model
    model = GrammarVAE()

    if torch.cuda.is_available():
        model.cuda()

    # optimizer, loss and annealing
    optimizer = torch.optim.Adam(model.parameters(), lr=params['learning_rate'])
    criterion = torch.nn.BCELoss()
    
    if params['anneal_kl']:
        anneal = AnnealKL(n_epoch=epochs, n_cycle=params['n_cycle'])

    # dict to save the results
    log = {'elbo': [], 'kl':[], 'reconstruction':[]}
    log_val = {'elbo': [], 'kl':[], 'reconstruction':[]}
    
    kl_weight = params['kl_weight']
    recons_weight = params['reconstruction_weight']

    for epoch in range(epochs):
      print(f"\n{'-' * 70}")
      print(f"epoch: {epoch+1}")
      
      model.train() 
    
      avg_elbo, avg_kl, avg_recons = 0, 0, 0
      for x, label in trainloader:
    
        # training procedure -----------------------------------------------------
        x = x.transpose(1, 2).contiguous().to(device)  # [batch, NUM_OF_RULES, MAX_LEN]
        z, mu, sigma, logits = model(x)

        # returning x to its original dimensions
        x = x.transpose(1, 2).contiguous()  # [batch, MAX_LEN, NUM_OF_RULES]
        x_decoded_mean = model.conditional(x, logits)  
    
        # calculating the errors
        reconstruction_loss = max_length * criterion(x_decoded_mean.view(-1), x.view(-1)) 
        kl = model.kl(mu, sigma)
    
        # annealing weigth beta to the kl
        if params['anneal_kl']:
            beta = anneal.beta(epoch)
            
            elbo = recons_weight * reconstruction_loss + kl_weight * kl * beta
            
        else:
            elbo = recons_weight * reconstruction_loss + kl_weight * kl
    
        # update parameters
        optimizer.zero_grad()
        elbo.backward()
        optimizer.step()
      
        # adding the error per batch
        avg_elbo += elbo.item()
        avg_kl += kl.item()
        avg_recons += reconstruction_loss.item()
    
      print('\n\033[1mTraining loss\033[0m')
      print(f'ELBO: {(avg_elbo/len(trainloader)):>5f} \tKL: {(avg_kl/len(trainloader)):>5f} \tReconstruction loss: {(avg_recons/len(trainloader)):>5f}') 
    
      # saving the results
      log['elbo'].append(avg_elbo/len(trainloader))
      log['kl'].append(avg_kl/len(trainloader))
      log['reconstruction'].append(avg_recons/len(trainloader))
      
      write_csv(log, os.path.join(saving_path, 'log.csv'))


################################################################################
      model.eval()
      with torch.no_grad():
          # validation procedure -----------------------------------------------------
          avg_elbo_val, avg_kl_val, avg_recons_val = 0, 0, 0
          for x_val, label_val in validloader:
        
            x_val = x_val.transpose(1, 2).contiguous().to(device)  # [batch, 76, 100]
            z_val, mu_val, sigma_val, logits_val = model(x_val)

            # returning x to its original dimensions
            x_val = x_val.transpose(1, 2).contiguous()  # [batch, 100, 76]
            x_decoded_mean_val = model.conditional(x_val, logits_val)  
        
            # calculating the errors
            reconstruction_loss_val = max_length * criterion(x_decoded_mean_val.view(-1), x_val.view(-1)) 
            kl_val = model.kl(mu_val, sigma_val)
            
            if params['anneal_kl']:
                elbo_val = params['reconstruction_weight'] * reconstruction_loss_val + params['kl_weigth'] * kl_val * beta
            else:
                elbo_val = params['reconstruction_weight'] * reconstruction_loss_val + params['kl_weigth'] * kl_val
    
            # adding the error per batch
            avg_elbo_val += elbo_val.item()
            avg_kl_val += kl_val.item()
            avg_recons_val += reconstruction_loss_val.item()
        
          print('\n\n\033[1mValidation loss\033[0m')
          print(f'ELBO_val: {(avg_elbo_val/len(validloader)):>5f} \tKL_val: {(avg_kl_val/len(validloader)):>5f} \tReconstruction_val: {(avg_recons_val/len(validloader)):>5f}' )
        
          if min_valid_loss > avg_elbo_val:
            print(f'Validation loss decreased ({(min_valid_loss/len(validloader)):.5f} ---> {(avg_elbo_val/len(validloader)):.5f}) \t Saving the model')
            min_valid_loss = avg_elbo_val
            
            torch.save(model.state_dict(), os.path.join(saving_path, 'gvae_model.pth'))
            # encoder
            torch.save(model.encoder.state_dict(), os.path.join(saving_path, 'gvae_encoder.pth'))
            # decoder
            torch.save(model.decoder.state_dict(), os.path.join(saving_path, 'gvae_decoder.pth'))
          
          log_val['elbo'].append(avg_elbo_val/len(validloader))
          log_val['kl'].append(avg_kl_val/len(validloader))
          log_val['reconstruction'].append(avg_recons_val/len(validloader))
        
          write_csv(log_val, os.path.join(saving_path, 'log_val.csv'))
      
   

if __name__ == "__main__":
    
    params = parameters.load_params()
    print(f'Parameters being used: {params}')
    
    if params['prop_pred']:
        prop_run(params)
    else:
        no_prop_run(params)
