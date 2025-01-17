from __future__ import print_function
import argparse
import os
import math
import random
import shutil
import psutil
import time 
import numpy as np
from tqdm import tqdm
import torch
import torch.optim as optim
import torch.nn.functional as F
import fake_opt
import scipy.io.wavfile
import librosa
import os

from options.train_options import TrainOptions
from model.enhance_model import EnhanceModel
from data.mix_data_loader import MixSequentialDataset, MixSequentialDataLoader, BucketingSampler
from utils.visualizer import Visualizer 
from utils import utils
from rewav import rewav

manualSeed = random.randint(1, 10000)
random.seed(manualSeed)
torch.manual_seed(manualSeed)
torch.cuda.manual_seed(manualSeed) 
# def rewav(path,uttid,feats,ang,hop_length = 256,win_length = 512,window = scipy.signal.hamming,rate = 16000):
#     if isinstance(feats,torch.Tensor):
#         feats_numpy = feats.numpy()
#         ang_numpy = ang.numpy()
#     else:
#         feats_numpy = feats
#         ang_numpy = ang
#     feat_mat = feats_numpy*np.cos(ang_numpy)+1j*feats_numpy*np.sin(ang_numpy)
#     #feat_mat = feat_mat[feat_mat]
#     feat_mat = feat_mat.T

#     x = librosa.core.istft(feat_mat,hop_length = hop_length,win_length = win_length, window = window)
#     x = x*65535
#     path_wav = os.path.join(path,"remix_"+uttid+".wav")
#     scipy.io.wavfile.write(path_wav,rate,data = x)
def main():
    
    opt = TrainOptions().parse()
    if opt.exp_path == None:
        opt = fake_opt.Enhance_base_train()  
    device = torch.device("cuda:{}".format(opt.gpu_ids[0]) if len(opt.gpu_ids) > 0 and torch.cuda.is_available() else "cpu")
 
    visualizer = Visualizer(opt)  
    logging = visualizer.get_logger()
    loss_report = visualizer.add_plot_report(['train/loss', 'val/loss'], 'loss.png')

    # data
    logging.info("Building dataset.")
    train_set = 'train'
    val_set = 'dev'
    train_dataset = MixSequentialDataset(opt, os.path.join(opt.dataroot, train_set), os.path.join(opt.dict_dir, 'train_units.txt'),train_set) 
    val_dataset   = MixSequentialDataset(opt, os.path.join(opt.dataroot, val_set), os.path.join(opt.dict_dir, 'train_units.txt'),val_set)
    train_sampler = BucketingSampler(train_dataset, batch_size=opt.batch_size) 
    train_loader = MixSequentialDataLoader(train_dataset, num_workers=opt.num_workers, batch_sampler=train_sampler)
    val_loader = MixSequentialDataLoader(val_dataset, batch_size=int(opt.batch_size/2), num_workers=opt.num_workers, shuffle=False)
    opt.idim = train_dataset.get_feat_size()
    opt.odim = train_dataset.get_num_classes()
    opt.char_list = train_dataset.get_char_list()
    opt.train_dataset_len = len(train_dataset)
    logging.info('#input dims : ' + str(opt.idim))
    logging.info('#output dims: ' + str(opt.odim))
    logging.info("Dataset ready!")
    save_wav_path = os.path.join(opt.checkpoints_dir,opt.name,'att_ws')
    
    lr = opt.lr
    eps = opt.eps
    iters = opt.iters    
    best_loss = opt.best_loss  
    start_epoch = opt.start_epoch      
    model_path = None
    if opt.enhance_resume:
        model_path = os.path.join(opt.works_dir, opt.enhance_resume)
        if os.path.isfile(model_path):
            package = torch.load(model_path, map_location=lambda storage, loc: storage)
            lr = package.get('lr', opt.lr)
            eps = package.get('eps', opt.eps)        
            best_loss = package.get('best_loss', float('inf'))
            start_epoch = int(package.get('epoch', 0))   
            iters = int(package.get('iters', 0))            
            loss_report = package.get('loss_report', loss_report)
            visualizer.set_plot_report(loss_report, 'loss.png')
            print('package found at {} and start_epoch {} iters {}'.format(model_path, start_epoch, iters))
        else:
            print("no checkpoint found at {}".format(model_path))     
    enhance_model = EnhanceModel.load_model(model_path, 'enhance_state_dict', opt)
                          
	# Setup an optimizer
    enhance_parameters = filter(lambda p: p.requires_grad, enhance_model.parameters())    
    if opt.opt_type == 'adadelta':
        enhance_optimizer = torch.optim.Adadelta(enhance_parameters, rho=0.95, eps=opt.eps)
    elif opt.opt_type == 'adam':
        enhance_optimizer = torch.optim.Adam(enhance_parameters, lr=opt.lr, betas=(opt.beta1, 0.999)) 
    # Training		                    
    for epoch in range(start_epoch, opt.epochs):
        enhance_model.train()
        if epoch > opt.shuffle_epoch:
            print("Shuffling batches for the following epochs")
            train_sampler.shuffle(epoch)  
            print("finish")
        for i, (data) in enumerate(train_loader, start=(iters % len(train_dataset))):
            utt_ids, spk_ids, clean_inputs, clean_log_inputs, mix_inputs, mix_log_inputs, cos_angles, targets, input_sizes, target_sizes,mix_angles,clean_angles,cmvn = data
            utt_id = utt_ids[0]
            # clean_input = clean_inputs[0].data
            # cos_angle = cos_angles[0].data
            # mix = mix_inputs[0].data
            # path_wav = '/usr/home/shi/projects/data_aishell/data/wavfile'
            # rewav(path_wav,utt_id,mix,cos_angle)
            loss, enhance_out = enhance_model( mix_inputs, mix_log_inputs, input_sizes,  clean_inputs,cos_angles) 
            enhance_optimizer.zero_grad()  # Clear the parameter gradients
            loss.backward()          
            # compute the gradient norm to check if it is normal or not
            grad_norm = torch.nn.utils.clip_grad_norm_(enhance_model.parameters(), opt.grad_clip)
            if math.isnan(grad_norm):
                logging.warning('grad norm is nan. Do not update model.')
            else:
                enhance_optimizer.step()
                
            iters += 1
            errors = {'train/loss': loss.item()}
            visualizer.set_current_errors(errors)
            if iters % opt.print_freq == 0:
                visualizer.print_current_errors(epoch, iters)
                state = {'enhance_state_dict': enhance_model.state_dict(), 'opt': opt,                                             
                         'epoch': epoch, 'iters': iters, 'eps': eps, 'lr': lr,                                    
                         'best_loss': best_loss, 'loss_report': loss_report}
                filename='latest'
                utils.save_checkpoint(state, opt.exp_path, filename=filename)
                    
            if iters % opt.validate_freq == 0:
                enhance_model.eval()
                torch.set_grad_enabled(False)
                num_saved_specgram = 0
                for i, (data) in tqdm(enumerate(val_loader, start=0)):
                    utt_ids, spk_ids, clean_inputs, clean_log_inputs, mix_inputs, mix_log_inputs, cos_angles, targets, input_sizes, target_sizes, clean_angles, mix_angles, cmvn = data
                    loss, enhance_out = enhance_model(mix_inputs, mix_log_inputs, input_sizes, clean_inputs, cos_angles)                 
                    errors = {'val/loss': loss.item()}
                    visualizer.set_current_errors(errors)
                
                    if opt.num_saved_specgram > 0:
                        if num_saved_specgram < opt.num_saved_specgram:
                            enhanced_outs = enhance_model.calculate_all_specgram(mix_inputs, mix_log_inputs, input_sizes)
                            for x in range(len(utt_ids)):
                                enhanced_out = enhanced_outs[x].data.cpu().numpy()
                                enhanced_out[enhanced_out <= 1e-7] = 1e-7
                                enhance_out_orig = enhanced_out
                                enhanced_out = np.log10(enhanced_out)
                                clean_input = clean_inputs[x].data.cpu().numpy()
                                clean_input[clean_input <= 1e-7] = 1e-7
                                clean_input_orig = clean_input
                                clean_input = np.log10(clean_input)
                                mix_input = mix_inputs[x].data.cpu().numpy()
                                mix_input[mix_input <= 1e-7] = 1e-7
                                mix_input_orig = mix_input
                                mix_input = np.log10(mix_input)
                                utt_id = utt_ids[x]
                                mix_angle = mix_angles[x].data.cpu().numpy()
                                input_size = int(input_sizes[x])
                                wav_name_mix = "{}_mix.wav".format(utt_id)
                                if not os.path.isfile(os.path.join(save_wav_path,wav_name_mix)):
                                    rewav(save_wav_path,utt_id,mix_input_orig,mix_angle,input_size = input_size,wav_file = wav_name_mix)
                                    #flag = 1
                                wav_name_enhance = "{}_ep{}_it{}_enhance.wav".format(utt_id, epoch, iters) 
                                file_name = "{}_ep{}_it{}.png".format(utt_id, epoch, iters)
                                rewav(save_wav_path,utt_id,enhance_out_orig,mix_angle,input_size = input_size,wav_file = wav_name_enhance)
                                visualizer.plot_specgram(clean_input, mix_input, enhanced_out, input_size, file_name)
                                num_saved_specgram += 1
                                if num_saved_specgram >= opt.num_saved_specgram:
                                    break                                                                                    
                enhance_model.train()
                torch.set_grad_enabled(True)  
				
                visualizer.print_epoch_errors(epoch, iters)               
                loss_report = visualizer.plot_epoch_errors(epoch, iters, 'loss.png')                     
                train_loss = visualizer.get_current_errors('train/loss')
                val_loss = visualizer.get_current_errors('val/loss')
                filename = None
                if val_loss > best_loss:
                    print('val_loss {} > best_loss {}'.format(val_loss, best_loss))
                    eps = utils.adadelta_eps_decay(optimizer, opt.eps_decay)
                else:
                    filename='model.loss.best'                                
                best_loss = min(val_loss, best_loss)
                print('best_loss {}'.format(best_loss))  
                
                state = {'enhance_state_dict': enhance_model.state_dict(), 'opt': opt,                                             
                         'epoch': epoch, 'iters': iters, 'eps': eps, 'lr': lr,                                    
                         'best_loss': best_loss, 'loss_report': loss_report}
                ##filename='epoch-{}_iters-{}_loss-{:.6f}-{:.6f}.pth'.format(epoch, iters, train_loss, val_loss)
                utils.save_checkpoint(state, opt.exp_path, filename=filename)
                visualizer.reset()      
      
if __name__ == '__main__':
    main()



# %%
