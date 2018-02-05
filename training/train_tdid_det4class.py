import torch
import torch.utils.data
import torchvision.models as models
import os
import sys
import importlib
import numpy as np
from datetime import datetime
import cv2
import time

from instance_detection.model_defs import network
from instance_detection.model_defs.TDID_det4class import TDID 

from instance_detection.utils.timer import Timer
from instance_detection.utils.utils import *
from instance_detection.utils.ILSVRC_VID_loader import VID_Loader

from instance_detection.testing.test_tdid_det4class import test_net, im_detect
from instance_detection.evaluation.COCO_eval.coco_det_eval import coco_det_eval 

import active_vision_dataset_processing.data_loading.active_vision_dataset_pytorch as AVD  

# load config
#cfg_file = 'configDET4UWC' #NO FILE EXTENSTION!
cfg_file = 'configGEN4UWC' #NO FILE EXTENSTION!
cfg = importlib.import_module('instance_detection.utils.configs.'+cfg_file)
cfg = cfg.get_config()



def validate_and_save(cfg,net,valset,target_images, epoch, iters):
    valloader = torch.utils.data.DataLoader(valset,
                                          batch_size=1,
                                          shuffle=True,
                                          collate_fn=AVD.collate)
    net.eval()
    model_name = cfg.MODEL_BASE_SAVE_NAME + '_{}'.format(epoch)
    acc = test_net(model_name, net, valloader, cfg.ID_TO_NAME, 
                           target_images,cfg.VAL_OBJ_IDS,cfg, 
                           max_dets_per_target=cfg.MAX_DETS_PER_TARGET,
                           output_dir=cfg.TEST_OUTPUT_DIR,
                           score_thresh=cfg.SCORE_THRESH)


    save_name = os.path.join(cfg.SNAPSHOT_SAVE_DIR, 
                             (cfg.MODEL_BASE_SAVE_NAME+
                              '_{}_{}_{:1.5f}_{:1.5f}.h5').format(
                             epoch,iters, epoch_loss/epoch_step_cnt, acc))
    network.save_net(save_name, net)
    print('save model: {}'.format(save_name))

    net.train()
    net.features.eval() #freeze batch norm layers?















##prepare target images (gather paths to the images)
target_images ={} 
if cfg.PYTORCH_FEATURE_NET:
    target_images = get_target_images(cfg.TARGET_IMAGE_DIR,cfg.NAME_TO_ID.keys())
else:
    print 'Must use pytorch pretrained model, others not supported'
    #would need to add new normaliztion to get_target_images, and elsewhere

#make sure only targets that have ids, and have target images are chosen
train_ids = check_object_ids(cfg.TRAIN_OBJ_IDS, cfg.ID_TO_NAME,target_images) 
cfg.TRAIN_OBJ_IDS = train_ids
val_ids = check_object_ids(cfg.VAL_OBJ_IDS, cfg.ID_TO_NAME,target_images) 
cfg.VAL_OBJ_IDS = val_ids
if train_ids==-1 or val_ids==-1:
    print 'Invalid IDS!'
    sys.exit()


print('Setting up training data...')
train_set = get_AVD_dataset(cfg.DATA_BASE_DIR,
                            cfg.TRAIN_LIST,
                            train_ids,
                            max_difficulty=cfg.MAX_OBJ_DIFFICULTY,
                            fraction_of_no_box=cfg.FRACTION_OF_NO_BOX_IMAGES)
valset = get_AVD_dataset(cfg.DATA_BASE_DIR,
                         cfg.VAL_LIST,
                         val_ids, 
                         max_difficulty=cfg.MAX_OBJ_DIFFICULTY,
                         fraction_of_no_box=cfg.VAL_FRACTION_OF_NO_BOX_IMAGES)

trainloader = torch.utils.data.DataLoader(train_set,
                                          batch_size=cfg.BATCH_SIZE,
                                          shuffle=True,
                                          num_workers=cfg.NUM_WORKERS,
                                          collate_fn=AVD.collate)
if cfg.USE_VID:
    vid_train_set = VID_Loader(cfg.VID_DATA_DIR,cfg.VID_SUBSET, 
                           target_size=cfg.VID_MAX_MIN_TARGET_SIZE, 
                           multiple_targets=True, 
                           batch_size=cfg.BATCH_SIZE)

print('Loading network...')
net = TDID(cfg)
if cfg.LOAD_FULL_MODEL:
    #load a previously trained model
    network.load_net(cfg.FULL_MODEL_LOAD_DIR + cfg.FULL_MODEL_LOAD_NAME, net)
else:
    network.weights_normal_init(net, dev=0.01)
    if cfg.USE_PRETRAINED_WEIGHTS:
        net.features = load_pretrained_weights(cfg.FEATURE_NET_NAME) 
net.features.eval()#freeze batchnorms layers?

#put net on gpu
net.cuda()
net.train()

#setup optimizer
params = list(net.parameters())
optimizer = torch.optim.SGD(params, lr=cfg.LEARNING_RATE,
                                    momentum=cfg.MOMENTUM, 
                                    weight_decay=cfg.WEIGHT_DECAY)

#make sure dir for saving model checkpoints exists
if not os.path.exists(cfg.SNAPSHOT_SAVE_DIR):
    os.mkdir(cfg.SNAPSHOT_SAVE_DIR)

# things to keep track of during training training
train_loss = 0
t = Timer()
t.tic()
total_iterations = 1 

write_training_meta(cfg,net)

print('Begin Training...')
for epoch in range(cfg.MAX_NUM_EPOCHS):
    targets_cnt = {}#how many times a target is used(visible, total)
    for cid in train_ids:
        targets_cnt[cid] = [0,0]
    epoch_loss = 0
    epoch_step_cnt = 0
    for step,batch in enumerate(trainloader):
        total_iterations += 1
        if cfg.BATCH_SIZE == 1:
            batch[0] = [batch[0]]
            batch[1] = [batch[1]]
        if type(batch[0]) is not list or len(batch[0]) < cfg.BATCH_SIZE:
            continue

        batch_im_data = []
        batch_target_data = []
        batch_gt_boxes = []
        for sample_ind in range(cfg.BATCH_SIZE):
            im_data=batch[0][sample_ind]
            if cfg.IMG_RESIZE >0:
                im_data = cv2.resize(im_data,(0,0),fx=cfg.IMG_RESIZE,fy=cfg.IMG_RESIZE)
            if cfg.AUGMENT_SCENE_IMAGES and np.random.rand() < .5:
                im_data = vary_image(im_data,crop_max=80,do_illum=False)
            im_data=normalize_image(im_data,cfg)
            gt_boxes = np.asarray(batch[1][sample_ind][0],dtype=np.float32) 
            if cfg.IMG_RESIZE >0  and gt_boxes.shape[0] >0:
                gt_boxes[:,:4] *= cfg.IMG_RESIZE 
            #if there are no boxes for this image, add a dummy background box
            if gt_boxes.shape[0] == 0:
                gt_boxes = np.asarray([[0,0,1,1,0]])

            objects_present = gt_boxes[:,4]
            objects_present = objects_present[np.where(objects_present!=0)[0]]
            not_present = np.asarray([ind for ind in train_ids 
                                              if ind not in objects_present and 
                                                 ind != 0]) 

            #pick a random target, with a bias towards choosing a target that 
            #is in the image. Also pick just that object's gt_box
            #if (np.random.rand() < .8 or not_present.shape[0]==0) and objects_present.shape[0]!=0:
            if (np.random.rand() < .5 or not_present.shape[0]==0) and objects_present.shape[0]!=0:
                target_ind = int(np.random.choice(objects_present))
                gt_boxes = gt_boxes[np.where(gt_boxes[:,4]==target_ind)[0],:-1] 
                gt_boxes[0,4] = 1
                targets_cnt[target_ind][0] += 1 
            else:#the target is not in the image, give a dummy background box
                target_ind = int(np.random.choice(not_present))
                gt_boxes = np.asarray([[0,0,1,1,0]])
            targets_cnt[target_ind][1] += 1 
            
            #get target images
            target_name = cfg.ID_TO_NAME[target_ind]
            target_data = []
            for t_type,_ in enumerate(target_images[target_name]):
                loaded_img = False
                while not loaded_img:
                    img_ind = np.random.choice(np.arange(
                                          len(target_images[target_name][t_type])))
                    target_img = cv2.imread(target_images[target_name][t_type][img_ind])
                    if target_img is not None:
                        loaded_img = True
                    else:
                        print target_images[target_name][t_type][img_ind]

                if np.random.rand() < .9 and cfg.AUGMENT_TARGET_IMAGES:
                    target_img = vary_image(target_img,do_illum=False)
                target_img = normalize_image(target_img,cfg)
                batch_target_data.append(target_img)

            batch_im_data.append(im_data)
            batch_gt_boxes.extend(gt_boxes)

        #prep data for input to network
        target_data = match_and_concat_images_list(batch_target_data,
                                                   min_size=cfg.MIN_TARGET_SIZE)
        im_data = match_and_concat_images_list(batch_im_data)
        gt_boxes = np.asarray(batch_gt_boxes) 

        im_info = im_data.shape[1:]
        im_data = network.np_to_variable(im_data, is_cuda=True)
        im_data = im_data.permute(0, 3, 1, 2)
        target_data = network.np_to_variable(target_data, is_cuda=True)
        target_data = target_data.permute(0, 3, 1, 2)

        # forward
        net(target_data, im_data, gt_boxes=gt_boxes, im_info=im_info)
        loss = net.loss * cfg.LOSS_MULT

        train_loss += loss.data[0]
        epoch_step_cnt += 1
        epoch_loss += loss.data[0]

        # backprop and parameter update
        optimizer.zero_grad()
        loss.backward()
        network.clip_gradient(net, 10.)
        optimizer.step()

        if cfg.USE_VID:
            temp_bs = cfg.BATCH_SIZE
            cfg.BATCH_SIZE = 2    
    
            batch = vid_train_set[0]
            gt_boxes = np.asarray(batch[1])
            im_data = match_and_concat_images_list(batch[0])
            im_data =  normalize_image(im_data,cfg)
            target_data = match_and_concat_images_list(batch[2]) 
            target_data = normalize_image(target_data,cfg)

            im_info = im_data.shape[1:]
            im_data = network.np_to_variable(im_data, is_cuda=True)
            im_data = im_data.permute(0, 3, 1, 2)
            target_data = network.np_to_variable(target_data, is_cuda=True)
            target_data = target_data.permute(0, 3, 1, 2)

            net(target_data, im_data, gt_boxes, im_info=im_info)
            loss = net.loss
            optimizer.zero_grad()
            loss.backward()
            network.clip_gradient(net, 10.)
            optimizer.step()

            cfg.BATCH_SIZE = temp_bs 

        #print out training info
        if step % cfg.DISPLAY_INTERVAL == 0:
            duration = t.toc(average=False)
            fps = step+1.0 / duration

            log_text = 'step %d, epoch_avg_loss: %.4f, fps: %.2f (%.2fs per batch) ' \
                       'epoch:%d loss: %.4f tot_avg_loss: %.4f %s' % (
                step,  epoch_loss/epoch_step_cnt, fps, 1./fps, 
                epoch, loss.data[0],train_loss/(step+1), cfg.MODEL_BASE_SAVE_NAME)
            print log_text
            print targets_cnt


        if (not cfg.SAVE_BY_EPOCH) and  total_iterations % cfg.SAVE_FREQ==0:
            validate_and_save(cfg,net,valset,target_images,epoch, total_iterations)
        

    ######################################################
    #epoch over
    if cfg.SAVE_BY_EPOCH and epoch % cfg.SAVE_FREQ == 0:
        validate_and_save(cfg,net,valset,target_images, epoch, total_iterations)

