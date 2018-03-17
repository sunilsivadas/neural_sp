#! /usr/bin/env python
# -*- coding: utf-8 -*-

"""Train the model (CSJ corpus)."""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import os
import sys
import time
from setproctitle import setproctitle
import argparse
from tensorboardX import SummaryWriter
from tqdm import tqdm

import torch
torch.manual_seed(1623)
torch.cuda.manual_seed_all(1623)

sys.path.append(os.path.abspath('../../../'))
from models.load_model import load
from examples.csj.s5.exp.dataset.load_dataset import Dataset
from examples.csj.s5.exp.metrics.cer import do_eval_cer
from examples.csj.s5.exp.metrics.wer import do_eval_wer
from utils.training.learning_rate_controller import Controller
from utils.training.plot import plot_loss
from utils.training.training_loop import train_step
from utils.training.logging import set_logger
from utils.directory import mkdir_join
from utils.config import load_config, save_config

MAX_DECODE_LEN_WORD = 60
MAX_DECODE_LEN_CHAR = 150

parser = argparse.ArgumentParser()
parser.add_argument('--gpu', type=int, default=-1,
                    help='the index of GPU (negative value indicates CPU)')
parser.add_argument('--config_path', type=str, default=None,
                    help='path to the configuration file')
parser.add_argument('--model_save_path', type=str, default=None,
                    help='path to save the model')
parser.add_argument('--saved_model_path', type=str, default=None,
                    help='path to the saved model to retrain')
parser.add_argument('--data_save_path', type=str, help='path to saved data')
parser.add_argument('--pretrain_stage', type=bool, default=False,
                    help='When True, start pre-training stage')


def main():

    args = parser.parse_args()

    ##################################################
    # DATSET
    ##################################################
    if args.model_save_path is not None:
        # Load a config file (.yml)
        params = load_config(args.config_path)
    # NOTE: Retrain the saved model from the last checkpoint
    elif args.saved_model_path is not None:
        params = load_config(os.path.join(args.saved_model_path, 'config.yml'))
    else:
        raise ValueError("Set model_save_path or saved_model_path.")

    # Load dataset
    train_data = Dataset(
        data_save_path=args.data_save_path,
        backend=params['backend'],
        input_channel=params['input_channel'],
        use_delta=params['use_delta'],
        use_double_delta=params['use_double_delta'],
        data_type='train', data_size=params['data_size'],
        label_type=params['label_type'],
        batch_size=params['batch_size'], max_epoch=params['num_epoch'],
        splice=params['splice'],
        num_stack=params['num_stack'], num_skip=params['num_skip'],
        min_frame_num=params['min_frame_num'],
        sort_utt=True, sort_stop_epoch=params['sort_stop_epoch'],
        tool=params['tool'], num_enque=None,
        dynamic_batching=params['dynamic_batching'])
    dev_data = Dataset(
        data_save_path=args.data_save_path,
        backend=params['backend'],
        input_channel=params['input_channel'],
        use_delta=params['use_delta'],
        use_double_delta=params['use_double_delta'],
        data_type='dev', data_size=params['data_size'],
        label_type=params['label_type'],
        batch_size=params['batch_size'], splice=params['splice'],
        num_stack=params['num_stack'], num_skip=params['num_skip'],
        shuffle=True, tool=params['tool'])
    eval1_data = Dataset(
        data_save_path=args.data_save_path,
        backend=params['backend'],
        input_channel=params['input_channel'],
        use_delta=params['use_delta'],
        use_double_delta=params['use_double_delta'],
        data_type='eval1', data_size=params['data_size'],
        label_type=params['label_type'],
        batch_size=params['batch_size'], splice=params['splice'],
        num_stack=params['num_stack'], num_skip=params['num_skip'],
        tool=params['tool'])
    eval2_data = Dataset(
        data_save_path=args.data_save_path,
        backend=params['backend'],
        input_channel=params['input_channel'],
        use_delta=params['use_delta'],
        use_double_delta=params['use_double_delta'],
        data_type='eval2', data_size=params['data_size'],
        label_type=params['label_type'],
        batch_size=params['batch_size'], splice=params['splice'],
        num_stack=params['num_stack'], num_skip=params['num_skip'],
        tool=params['tool'])
    eval3_data = Dataset(
        data_save_path=args.data_save_path,
        backend=params['backend'],
        input_channel=params['input_channel'],
        use_delta=params['use_delta'],
        use_double_delta=params['use_double_delta'],
        data_type='eval3', data_size=params['data_size'],
        label_type=params['label_type'],
        batch_size=params['batch_size'], splice=params['splice'],
        num_stack=params['num_stack'], num_skip=params['num_skip'],
        tool=params['tool'])

    params['num_classes'] = train_data.num_classes

    ##################################################
    # MODEL
    ##################################################
    if args.model_save_path is not None:
        # Model setting
        model = load(model_type=params['model_type'],
                     params=params,
                     backend=params['backend'])

        # Set save path
        save_path = mkdir_join(
            args.model_save_path, params['backend'],
            params['model_type'], params['label_type'], params['data_size'], model.name)
        model.set_save_path(save_path)

        # Save config file
        save_config(config_path=args.config_path, save_path=model.save_path)

        # Setting for logging
        logger = set_logger(model.save_path)

        # Count total parameters
        for name in sorted(list(model.num_params_dict.keys())):
            num_params = model.num_params_dict[name]
            logger.info("%s %d" % (name, num_params))
        logger.info("Total %.3f M parameters" %
                    (model.total_parameters / 1000000))

        # Define optimizer
        model.set_optimizer(
            optimizer=params['optimizer'],
            learning_rate_init=float(params['learning_rate']),
            weight_decay=float(params['weight_decay']),
            clip_grad_norm=params['clip_grad_norm'],
            lr_schedule=False,
            factor=params['decay_rate'],
            patience_epoch=params['decay_patient_epoch'])

        epoch, step = 1, 0
        learning_rate = float(params['learning_rate'])
        metric_dev_best = 1

    # NOTE: Retrain the saved model from the last checkpoint
    elif args.saved_model_path is not None:
        # Load model
        model = load(model_type=params['model_type'],
                     params=params,
                     backend=params['backend'])

        # Set save path
        model.save_path = args.saved_model_path

        # Setting for logging
        logger = set_logger(model.save_path, restart=True)

        # Define optimizer
        model.set_optimizer(
            optimizer=params['optimizer'],
            learning_rate_init=float(params['learning_rate']),
            weight_decay=float(params['weight_decay']),
            clip_grad_norm=params['clip_grad_norm'],
            lr_schedule=False,
            factor=params['decay_rate'],
            patience_epoch=params['decay_patient_epoch'])

        # Restore the last saved model
        epoch, step, learning_rate, metric_dev_best = model.load_checkpoint(
            save_path=args.saved_model_path, epoch=-1, restart=True)

    else:
        raise ValueError("Set model_save_path or saved_model_path.")

    train_data.epoch = epoch - 1

    # GPU setting
    model.set_cuda(deterministic=False, benchmark=True)

    logger.info('PID: %s' % os.getpid())
    logger.info('USERNAME: %s' % os.uname()[1])

    # Set process name
    setproctitle('csj_' + params['model_type'] + '_' +
                 params['label_type'] + '_' + params['data_size'])

    ##################################################
    # TRAINING LOOP
    ##################################################
    # Define learning rate controller
    lr_controller = Controller(
        learning_rate_init=params['learning_rate'],
        backend=params['backend'],
        decay_start_epoch=params['decay_start_epoch'],
        decay_rate=params['decay_rate'],
        decay_patient_epoch=params['decay_patient_epoch'],
        lower_better=True)

    # Setting for tensorboard
    if params['backend'] == 'pytorch':
        tf_writer = SummaryWriter(model.save_path)

    # Train model
    csv_steps, csv_loss_train, csv_loss_dev = [], [], []
    start_time_train = time.time()
    start_time_epoch = time.time()
    start_time_step = time.time()
    not_improved_epoch = 0
    loss_train_mean = 0.
    pbar_epoch = tqdm(total=len(train_data))
    while True:
        # Compute loss in the training set (including parameter update)
        batch_train, is_new_epoch = train_data.next()
        model, loss_train = train_step(
            model, batch_train, params['clip_grad_norm'], params['backend'])
        loss_train_mean += loss_train

        pbar_epoch.update(len(batch_train['xs']))

        if (step + 1) % params['print_step'] == 0:

            # Compute loss in the dev set
            batch_dev = dev_data.next()[0]
            loss_dev = model(
                batch_dev['xs'], batch_dev['ys'],
                batch_dev['x_lens'], batch_dev['y_lens'], is_eval=True)

            loss_train_mean /= params['print_step']
            csv_steps.append(step)
            csv_loss_train.append(loss_train_mean)
            csv_loss_dev.append(loss_dev)

            # Logging by tensorboard
            if params['backend'] == 'pytorch':
                tf_writer.add_scalar('train/loss', loss_train_mean, step + 1)
                tf_writer.add_scalar('dev/loss', loss_dev, step + 1)
                for name, param in model.named_parameters():
                    name = name.replace('.', '/')
                    tf_writer.add_histogram(
                        name, param.data.cpu().numpy(), step + 1)
                    tf_writer.add_histogram(
                        name + '/grad', param.grad.data.cpu().numpy(), step + 1)

            duration_step = time.time() - start_time_step
            logger.info("...Step:%d(epoch:%.3f) loss:%.3f(%.3f)/lr:%.5f/batch:%d/x_lens:%d (%.3f min)" %
                        (step + 1, train_data.epoch_detail,
                         loss_train_mean, loss_dev,
                         learning_rate, train_data.current_batch_size,
                         max(batch_train['x_lens']) * params['num_stack'],
                         duration_step / 60))
            start_time_step = time.time()
            loss_train_mean = 0.
        step += 1

        # Save checkpoint and evaluate model per epoch
        if is_new_epoch:
            duration_epoch = time.time() - start_time_epoch
            logger.info('===== EPOCH:%d (%.3f min) =====' %
                        (epoch, duration_epoch / 60))

            # Save fugure of loss
            plot_loss(csv_loss_train, csv_loss_dev, csv_steps,
                      save_path=model.save_path)

            if epoch < params['eval_start_epoch']:
                # Save the model
                model.save_checkpoint(model.save_path, epoch, step,
                                      learning_rate, metric_dev_best)
            else:
                start_time_eval = time.time()
                # dev
                if 'word' in params['label_type'] and not bool(param['pretrain_stage']):
                    metric_dev_epoch, _ = do_eval_wer(
                        model=model,
                        dataset=dev_data,
                        beam_width=1,
                        max_decode_len=MAX_DECODE_LEN_WORD,
                        eval_batch_size=1)
                    logger.info('  WER (dev): %.3f %%' %
                                (metric_dev_epoch * 100))
                else:
                    metric_dev_epoch, wer_dev_epoch, _ = do_eval_cer(
                        model=model,
                        dataset=dev_data,
                        beam_width=1,
                        max_decode_len=MAX_DECODE_LEN_CHAR,
                        eval_batch_size=1)
                    logger.info('  CER / WER (dev): %.3f %% / %.3f %%' %
                                ((metric_dev_epoch * 100), (wer_dev_epoch * 100)))

                if metric_dev_epoch < metric_dev_best:
                    metric_dev_best = metric_dev_epoch
                    not_improved_epoch = 0
                    logger.info('||||| Best Score |||||')

                    # Save the model
                    model.save_checkpoint(model.save_path, epoch, step,
                                          learning_rate, metric_dev_best)

                    # test
                    if 'word' in params['label_type'] and not bool(params['pretrain_stage']):
                        wer_eval1, _ = do_eval_wer(
                            model=model,
                            dataset=eval1_data,
                            beam_width=1,
                            max_decode_len=MAX_DECODE_LEN_CHAR,
                            eval_batch_size=1)
                        logger.info('  WER (eval1): %.3f %%' %
                                    (wer_eval1 * 100))

                        wer_eval2, _ = do_eval_wer(
                            model=model,
                            dataset=eval2_data,
                            beam_width=1,
                            max_decode_len=MAX_DECODE_LEN_CHAR,
                            eval_batch_size=1)
                        logger.info('  WER (eval2): %.3f %%' %
                                    (wer_eval2 * 100))

                        wer_eval3, _ = do_eval_wer(
                            model=model,
                            dataset=eval3_data,
                            beam_width=1,
                            max_decode_len=MAX_DECODE_LEN_CHAR,
                            eval_batch_size=1)
                        logger.info('  WER (eval3): %.3f %%' %
                                    (wer_eval3 * 100))

                        logger.info('  WER (mean): %.3f %%' %
                                    ((wer_eval1 + wer_eval2 + wer_eval3) * 100 / 3))
                    else:
                        cer_eval1, wer_eval1, _ = do_eval_cer(
                            model=model,
                            dataset=eval1_data,
                            beam_width=1,
                            max_decode_len=MAX_DECODE_LEN_CHAR,
                            eval_batch_size=1)
                        logger.info('  CER / WER (eval1): %.3f %% / %.3f %%' %
                                    ((cer_eval1 * 100), (wer_eval1 * 100)))

                        cer_eval2, wer_eval2, _ = do_eval_cer(
                            model=model,
                            dataset=eval2_data,
                            beam_width=1,
                            max_decode_len=MAX_DECODE_LEN_CHAR,
                            eval_batch_size=1)
                        logger.info('  CER / WER (eval2): %.3f %% / %.3f %%' %
                                    ((cer_eval2 * 100), (wer_eval2 * 100)))

                        cer_eval3, wer_eval3, _ = do_eval_cer(
                            model=model,
                            dataset=eval3_data,
                            beam_width=1,
                            max_decode_len=MAX_DECODE_LEN_CHAR,
                            eval_batch_size=1)
                        logger.info('  CER / WER (eval3): %.3f %% / %.3f %%' %
                                    ((cer_eval3 * 100), (wer_eval3 * 100)))

                        logger.info('  CER / WER (mean): %.3f %% / %.3f %%' %
                                    (((cer_eval1 + cer_eval2 + cer_eval3) * 100 / 3),
                                     ((wer_eval1 + wer_eval2 + wer_eval3) * 100 / 3)))
                else:
                    not_improved_epoch += 1

                duration_eval = time.time() - start_time_eval
                logger.info('Evaluation time: %.3f min' % (duration_eval / 60))

                # Early stopping
                if not_improved_epoch == params['not_improved_patient_epoch']:
                    break

                # Update learning rate
                model.optimizer, learning_rate = lr_controller.decay_lr(
                    optimizer=model.optimizer,
                    learning_rate=learning_rate,
                    epoch=epoch,
                    value=metric_dev_epoch)

                if epoch == params['convert_to_sgd_epoch']:
                    # Convert to fine-tuning stage
                    model.set_optimizer(
                        'sgd',
                        learning_rate_init=learning_rate,
                        weight_decay=float(params['weight_decay']),
                        clip_grad_norm=params['clip_grad_norm'],
                        lr_schedule=False,
                        factor=params['decay_rate'],
                        patience_epoch=params['decay_patient_epoch'])
                    logger.info('========== Convert to SGD ==========')

                    # Inject Gaussian noise to all parameters
                    if float(params['weight_noise_std']) > 0:
                        model.weight_noise_injection = True

            pbar_epoch = tqdm(total=len(train_data))
            print('========== EPOCH:%d (%.3f min) ==========' %
                  (epoch, duration_epoch / 60))

            if epoch == params['num_epoch']:
                break

            start_time_step = time.time()
            start_time_epoch = time.time()
            epoch += 1

    duration_train = time.time() - start_time_train
    logger.info('Total time: %.3f hour' % (duration_train / 3600))

    if params['backend'] == 'pytorch':
        tf_writer.close()

    # Training was finished correctly
    with open(os.path.join(model.save_path, 'COMPLETE'), 'w') as f:
        f.write('')


if __name__ == '__main__':
    main()