"""
@version: 1.0
@author: Chao Chen
@contact: chao.chen@sjtu.edu.cn
"""
import argparse
import functools
import logging.config
import os
import random

import numpy as np
import torch as th
import torch.nn as nn
import yaml
from torch.utils.data import DataLoader

from data.masking import CorrelationAdjustedMask
from data.traffic import METRDataset
from data.traffic import Reader
from data.traffic import collate_fn, collate_mask
from helper import TrafficForecasting
from util import EarlyStoppingV3, WallClock


def args():
    parser = argparse.ArgumentParser(description='EasyDGL Benchmark')
    parser.add_argument('--config', default='conf/model/METR-LA/EasyDGL.yaml', type=str,
                        help='Config file.')
    parser.add_argument('--seed', type=int, default=9876)
    parser.add_argument('--device', type=int, default=0,
                        help='running device. E.g `--device 0`, if using cpu, set `--device -1`')
    return parser.parse_args()


def evaluate(net, graph, dataloader, scaler):
    num_timesteps_out = config['data'].get('num_timesteps_out', 12)
    MAE = np.zeros(num_timesteps_out)
    RMSE = np.zeros(num_timesteps_out)
    MAPE = np.zeros(num_timesteps_out)
    T = np.zeros(num_timesteps_out)

    net.eval()
    with th.no_grad():
        for feat, label in dataloader:
            feat = {k: v.to(device) for k, v in feat.items()}
            y_true = label[..., 0].to(device)
            if th.sum(y_true).item() == 0.:
                continue

            feat['x'] = scaler.transform(feat['x'])

            # Forward-inference
            y_pred = net.forward(graph, feat)
            y_pred = scaler.inverse_transform(y_pred)

            mask = th.sign(y_true)
            mae = th.abs(y_pred - y_true) * mask
            rmse = th.square(y_pred - y_true) * mask
            mape = mae / th.where(y_true == 0., th.ones_like(y_true), y_true)

            T += th.sum(mask, dim=(0, 2)).cpu().numpy()
            MAE += th.sum(mae, dim=(0, 2)).cpu().numpy()
            RMSE += th.sum(rmse, dim=(0, 2)).cpu().numpy()
            MAPE += th.sum(mape, dim=(0, 2)).cpu().numpy()

    return {'MAE': MAE / T, 'RMSE': np.sqrt(RMSE / T), 'MAPE': MAPE / T}


def run():
    data_config = config['data']
    reader = Reader(data_config['fpath'])
    graph = reader.g.to(device)
    train_data = METRDataset(reader.train_data)
    valid_data = METRDataset(reader.valid_data)
    test_data = METRDataset(reader.test_data)

    batch_size = data_config.get('batch_size')
    mask_rate = data_config.get('mask_rate')
    mask_sep = data_config.get('mask_sep')
    mask_max = data_config.get('mask_max')
    logging.info("======data configure======")
    logging.info(f"batch_size: {batch_size}")
    logging.info(f"mask_rate: {mask_rate}")
    logging.info(f"mask_sep: {mask_sep}")
    logging.info(f"mask_max: {mask_max}")
    mask_fn = CorrelationAdjustedMask(mask_rate, mask_sep, mask_max)
    collate_mask_fn = functools.partial(collate_mask, mask_fn=mask_fn)
    train_dataloader = DataLoader(train_data, batch_size=batch_size, num_workers=4,
                                  shuffle=True, collate_fn=collate_mask_fn, pin_memory=True)
    valid_dataloader = DataLoader(valid_data, batch_size=batch_size,
                                  shuffle=False, collate_fn=collate_fn)
    test_dataloader = DataLoader(test_data, batch_size=batch_size,
                                 shuffle=False, collate_fn=collate_fn)

    training_config = config['train']
    patience = training_config.get('patience')
    test_every_n_epochs = training_config.get('test_every_n_epochs')
    epochs = training_config.get('epochs')
    lr = training_config.get('lr')
    eps = training_config.get('eps', 1e-8)
    lr_decay_ratio = training_config.get('lr_decay_ratio', 1.)
    weight_decay = training_config.get('weight_decay', 0.)
    grad_clip = training_config.get('max_grad_norm')
    logging.info("======train configure======")
    logging.info(f"learning rate: {lr}")
    logging.info(f"eps in Adam: {eps}")
    logging.info(f"learning decay ratio: {lr_decay_ratio}")
    logging.info(f"l2 weight decay: {weight_decay}")
    logging.info(f"epochs: {epochs}")

    net, optim, scheduler, scaler = TrafficForecasting.build(config, reader)
    net = net.to(device)

    wallclock = WallClock()
    stopper = EarlyStoppingV3(patience)
    for epoch in range(epochs + 1):
        running_loss = list()

        # training stage
        wallclock.tik()
        net.train()
        for feat, label in train_dataloader:
            feat = {k: v.to(device) for k, v in feat.items()}
            y_true = label[..., 0].to(device)

            feat['x'] = scaler.transform(feat['x'])
            # Here for Curriculum Learning
            feat['y'] = scaler.transform(feat['y'])

            # Forward-inference
            y_pred = net.forward(graph, feat)
            y_pred = scaler.inverse_transform(y_pred)

            loss = net.loss(y_pred, y_true)
            running_loss.append(loss.item())

            # Back-propogation
            optim.zero_grad(set_to_none=True)
            loss.backward()
            nn.utils.clip_grad_norm_(net.parameters(), grad_clip)
            optim.step()
        wallclock.tok()

        scheduler.step()  # update the learning rate
        logging.info("{0:3d}, loss={1:5f}, lr={2:5f} -- {3:.1f}s".format(
            epoch, np.mean(running_loss), scheduler.get_last_lr()[0], wallclock.elapse()))

        # evaluation stage
        if epoch % test_every_n_epochs == 0:
            valid_res = evaluate(net, graph, valid_dataloader, scaler)
            logging.info(f"-3H: [{valid_res['MAE'][2]:.3f}, {valid_res['RMSE'][2]:.3f}, {valid_res['MAPE'][2]:.4f}] "
                         f"-6H: [{valid_res['MAE'][5]:.3f}, {valid_res['RMSE'][5]:.3f}, {valid_res['MAPE'][5]:.4f}] "
                         f"-9H: [{valid_res['MAE'][8]:.3f}, {valid_res['RMSE'][8]:.3f}, {valid_res['MAPE'][8]:.4f}] "
                         f"-12H: [{valid_res['MAE'][11]:.3f}, {valid_res['RMSE'][11]:.3f}, "
                         f"{valid_res['MAPE'][11]:.4f}]")
            test_res = evaluate(net, graph, test_dataloader, scaler)
            logging.info(f"-3H: [{test_res['MAE'][2]:.3f}, {test_res['RMSE'][2]:.3f}, {test_res['MAPE'][2]:.4f}] "
                         f"-6H: [{test_res['MAE'][5]:.3f}, {test_res['RMSE'][5]:.3f}, {test_res['MAPE'][5]:.4f}] "
                         f"-9H: [{test_res['MAE'][8]:.3f}, {test_res['RMSE'][8]:.3f}, {test_res['MAPE'][8]:.4f}] "
                         f"-12H: [{test_res['MAE'][11]:.3f}, {test_res['RMSE'][11]:.3f}, "
                         f"{test_res['MAPE'][11]:.4f}]")
            stopper.step(running_loss, valid_res['MAE'].mean(), valid_res, test_res)
            if stopper.early_stop:
                break
    stopper.summary()
    wallclock.summary()


if __name__ == "__main__":
    logging.config.fileConfig('./conf/logging.conf')
    flags = args()

    SEED = flags.seed
    np.random.seed(SEED)
    random.seed(SEED)
    th.manual_seed(SEED)
    th.backends.cudnn.benchmark = False
    th.backends.cudnn.deterministic = True
    os.environ['PYTHONHASHSEED'] = str(SEED)

    config = yaml.load(open(flags.config, 'r'), yaml.Loader)
    device = th.device(flags.device) if flags.device >= 0 else th.device('cpu')

    run()
