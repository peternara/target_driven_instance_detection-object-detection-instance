import os
import torch
import torchvision.models as models
import cv2
import cPickle
import numpy as np
import importlib

from instance_detection.model_defs import network
from instance_detection.model_defs.TDID_class import TDID
from instance_detection.model_defs.fast_rcnn.nms_wrapper import nms

from instance_detection.utils.timer import Timer
from instance_detection.utils.utils import * 


from instance_detection.model_defs.fast_rcnn.bbox_transform import bbox_transform_inv, clip_boxes
from instance_detection.model_defs.fast_rcnn.config import cfg, cfg_from_file, get_output_dir

import active_vision_dataset_processing.data_loading.active_vision_dataset_pytorch as AVD  

#import matplotlib.pyplot as plt
import json




def im_detect(net, target_data,im_data, im_info, features_given=True):
    """Detect object classes in an image given object proposals.
    Returns:
        scores (ndarray): R x K array of object class scores (K includes
            background as object category 0)
        boxes (ndarray): R x (4*K) array of predicted bounding boxes
    """


    scores = net(target_data, im_data, 
                                    features_given=features_given, im_info=im_info)
    return scores


def test_net(model_name, net, dataloader, id_to_name, target_images, chosen_ids, cfg,
             max_dets_per_target=5, score_thresh=0.1,
             output_dir=None,):
    """Test a TDID network on an image dataset."""
    #list to output for coco evaluation
    results = []
 
    #num images in test set
    num_images = len(dataloader)
   
    # timers
    _t = {'im_detect': Timer(), 'misc': Timer()}
    
    if output_dir is not None:
        det_file = os.path.join(output_dir, model_name+'.json')
        print det_file


    #pre compute features for all targets
    target_features_dict = {}
    target_data_dict = {}
    for id_ind,t_id in enumerate(chosen_ids):
        target_name = id_to_name[t_id]
        if target_name == 'background':
            continue
        target_data = []
        for t_type,_ in enumerate(target_images[target_name]):
            img_ind = np.random.choice(np.arange(
                                  len(target_images[target_name][t_type])))
            target_img = cv2.imread(target_images[target_name][t_type][img_ind])
            target_img = normalize_image(target_img,cfg)
            target_data.append(target_img)

        target_data = match_and_concat_images_list(target_data)
        target_data = network.np_to_variable(target_data, is_cuda=True)
        target_data = target_data.permute(0, 3, 1, 2)
        if cfg.TEST_ONE_AT_A_TIME:
            target_data_dict[target_name] = target_data
        else:
            target_features_dict[target_name] = net.features(target_data)



    total = 0
    total_correct = 0
    #for i in range(num_images):
    for i,batch in enumerate(dataloader):
        if i>100:
            break
        if len(batch[0]) == 0:
            print batch
            continue
        im_data= batch[0][0]
        im_info = im_data.shape[:]
        im_data=normalize_image(im_data,cfg)
        im_data = network.np_to_variable(im_data, is_cuda=True)
        im_data = im_data.unsqueeze(0)
        im_data = im_data.permute(0, 3, 1, 2)

        print '{}/{}'.format(i, len(dataloader))

        #get ground truth id
        gt_id = batch[1][0][0]

        #get image name and index
        img_name = batch[1][1]
        img_ind = int(img_name[:-4])

        #get image features
        if not cfg.TEST_ONE_AT_A_TIME:
            img_features = net.features(im_data)

        max_score = 0
        max_id = -1
        true_object_score = 0
        for id_ind,t_id in enumerate(chosen_ids):
            target_name = id_to_name[t_id]
            if target_name == 'background':
                continue

            if cfg.TEST_ONE_AT_A_TIME:
                target_data = target_data_dict[target_name]
                _t['im_detect'].tic()
                scores = im_detect(net, target_data, im_data, im_info,
                                          features_given=False)
                detect_time = _t['im_detect'].toc(average=False)
            else:
                target_features = target_features_dict[target_name]
                _t['im_detect'].tic()
                scores = im_detect(net, target_features, img_features, im_info)
                detect_time = _t['im_detect'].toc(average=False)

            _t['misc'].tic()
            
            scores = scores.data.cpu().squeeze().numpy()
            correct = False 
            if scores[1]>max_score:
                max_score = scores[1]
                max_id = t_id
            if t_id == gt_id:
                true_object_score = scores[1] 

        #if true_object_score >= max_score:
        if max_id == gt_id:
            total_correct +=1
        total+=1
        print 'maxid: {} gt: {} maxscore: {} gtscore: {}'.format(max_id, gt_id, max_score, true_object_score)
    #if output_dir is not None:
    #    with open(det_file, 'w') as f:
    #        json.dump(results,f)


    print '{}  {}  {}'.format(total_correct, total, float(total_correct)/float(total))    
    return float(total_correct)/float(total) 






if __name__ == '__main__':

    #load config file
    cfg_file = 'configUW_class' #NO EXTENSTION!
    cfg = importlib.import_module('instance_detection.utils.configs.'+cfg_file)
    cfg = cfg.get_config()

    ##prepare target images (gather paths to the images)
    target_images ={}
    if cfg.PYTORCH_FEATURE_NET:
        target_images = get_target_images(cfg.TARGET_IMAGE_DIR,cfg.NAME_TO_ID.keys())
    else:
        print 'Must use pytorch pretrained model, others not supported'
        #would need to add new normaliztion to get_target_images, and elsewhere

    #make sure only targets that have ids, and have target images are chosen
    test_ids = check_object_ids(cfg.TEST_OBJ_IDS, cfg.ID_TO_NAME,target_images)
    if test_ids==-1:
        print 'Invalid IDS!'
        sys.exit()

    testset = get_AVD_dataset(cfg.DATA_BASE_DIR,
                              cfg.TEST_LIST,
                              test_ids,
                              max_difficulty=cfg.MAX_OBJ_DIFFICULTY,
                              fraction_of_no_box=cfg.TEST_FRACTION_OF_NO_BOX_IMAGES,
                              classification=True)


    batch = testset[0]

    #create train/test loaders, with CUSTOM COLLATE function
    testloader = torch.utils.data.DataLoader(testset,
                                              batch_size=1,
                                              shuffle=True,
                                              num_workers=cfg.NUM_WORKERS,
                                              collate_fn=AVD.collate)

    # load net
    print('Loading ' + cfg.FULL_MODEL_LOAD_NAME + ' ...')
    net = TDID(cfg)
    network.load_net(cfg.FULL_MODEL_LOAD_DIR + cfg.FULL_MODEL_LOAD_NAME, net)
    net.features.eval()#freeze batchnorms layers?
    print('load model successfully!')
    
    net.cuda()
    net.eval()
    
    # evaluation
    test_net(cfg.MODEL_BASE_SAVE_NAME, net, testloader, cfg.ID_TO_NAME, 
    	 target_images,test_ids,cfg, 
    	 max_dets_per_target=cfg.MAX_DETS_PER_TARGET,
    	 score_thresh=cfg.SCORE_THRESH, 
    	 output_dir=cfg.TEST_OUTPUT_DIR)




