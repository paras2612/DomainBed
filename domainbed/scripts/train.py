# Copyright (c) Facebook, Inc. and its affiliates. All Rights Reserved

import argparse
import collections
import json
import os
import random
import sys
import time
import uuid

import numpy as np
import PIL
import torch
from torch import nn, optim
import torch.nn.functional as F
import torchvision
import torch.utils.data

from domainbed import datasets
from domainbed import hparams_registry
from domainbed import algorithms
from domainbed.lib import misc
from domainbed.lib.fast_data_loader import InfiniteDataLoader, FastDataLoader

from domainbed.networks import FeaturizerDropout, Classifier

device = 'cuda' if torch.cuda.is_available() else 'cpu'

def mean_nll(logits, y):
    return nn.functional.binary_cross_entropy_with_logits(logits, y)


# TODO fix this for multiclass classification
def mean_accuracy(logits, y):
    pred_class = torch.argmax(logits, dim=1)
    return (pred_class==y).float().mean()


def self_distill(hparams, input_shape, num_classes, trained_model, train_loaders, test_loaders, steps, lr, deepness=6, num_confirmations=10, filter_ratio=0.3):
    # define the current model
    num_epochs = 10
    current_model = trained_model
    for d in range(deepness):
        pred_history = []
        pred_logits_history = []
        # x = []
        # y = []
        # for z in test_loaders:
        #     for i, (m_x, m_y) in enumerate(z):
        #         x.append(m_x)
        #         y.append(m_y)
        #         if i>data_limit:
        #             break

        # test_data = [(x, y) for z in test_loaders for x, y in z]
        # x, y = list(zip(*test_data))
        # x = [y for x in test_envs for y in x['images']]
        # y = [y for x in test_envs for y in x['labels']]
        # x = torch.stack(x)
        # y = torch.stack(y)

        # TODO incorporate test data


        # define the distilled model
        # sample_train_x, _ = next(iter(train_loaders[0]))
        featurizer = FeaturizerDropout(input_shape, hparams)
        classifier = Classifier(featurizer.n_outputs, num_classes, hparams['nonlinear_classifier'])
        distilled_model = torch.nn.Sequential(featurizer, classifier).to(device) #.cuda()
        optimizer = optim.Adam(distilled_model.parameters(), lr=lr)

        # pseudo label the test data
        x = []
        y = []
        for j in test_loaders:
            for i, (m_x, m_y) in enumerate(j):
                x.append(m_x)
                y.append(m_y)
                if i > num_epochs:
                    break
        x = torch.vstack(x)
        y = torch.cat(y)

        for i in range(num_confirmations):
            with torch.no_grad():
                pred_logits = current_model(x).detach()
                pred_history.append(torch.argmax(pred_logits, axis=1))
                pred_logits_history.append((pred_logits))

        # filter the top confidence samples from the test data
        pred_history = torch.stack(pred_history)
        pred_logits_history = torch.stack(pred_logits_history)
        pred_variation = pred_history.float().var(axis=0)

        mean_prediction = torch.mean(pred_logits_history, dim = 0)
        modal_prediction = torch.mode(pred_history, dim=0).values

        hci = torch.sort(pred_variation.flatten()).indices
        num_filtered_samples = int(len(hci) * filter_ratio)

        zero_var_sample_indices = torch.where(pred_variation == 0)[0]
        if len(zero_var_sample_indices) > num_filtered_samples:
            hci = zero_var_sample_indices
            num_filtered_samples = len(zero_var_sample_indices)

        new_x = x[hci[:num_filtered_samples]]
        new_y = modal_prediction[hci[:num_filtered_samples]]
        new_gt_y = y[hci[:num_filtered_samples]]

        # For logging purposes, print the accuracy of the most confident predictions


        # train on the top confidence samples
        for step in range(steps):
            logits = distilled_model(new_x)
            nll = F.cross_entropy(logits, new_y)  # mean_nll(logits, new_y)
            optimizer.zero_grad()
            nll.backward()
            optimizer.step()
            if step % 300 == 0:
                # print('=' * 10)
                distilled_model.eval()
                print(f'--> Test Accuracy: {mean_accuracy(distilled_model(x).detach(), y)}')
                distilled_model.train()

        # update the current model
        current_model = distilled_model
        print(f'Final Test Accuracy: {mean_accuracy(distilled_model(x).detach(), y)}')
        pass
    return current_model


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description='Domain generalization')
    parser.add_argument('--data_dir', type=str)
    parser.add_argument('--dataset', type=str, default="RotatedMNIST")
    parser.add_argument('--algorithm', type=str, default="ERM")
    parser.add_argument('--task', type=str, default="domain_generalization",
        choices=["domain_generalization", "domain_adaptation"])
    parser.add_argument('--hparams', type=str,
        help='JSON-serialized hparams dict')
    parser.add_argument('--hparams_seed', type=int, default=0,
        help='Seed for random hparams (0 means "default hparams")')
    parser.add_argument('--trial_seed', type=int, default=0,
        help='Trial number (used for seeding split_dataset and '
        'random_hparams).')
    parser.add_argument('--seed', type=int, default=0,
        help='Seed for everything else')
    parser.add_argument('--steps', type=int, default=None,
        help='Number of steps. Default is dataset-dependent.')
    parser.add_argument('--checkpoint_freq', type=int, default=None,
        help='Checkpoint every N steps. Default is dataset-dependent.')
    parser.add_argument('--test_envs', type=int, nargs='+', default=[0])
    parser.add_argument('--output_dir', type=str, default="train_output")
    parser.add_argument('--holdout_fraction', type=float, default=0.2)
    parser.add_argument('--uda_holdout_fraction', type=float, default=0,
        help="For domain adaptation, % of test to use unlabeled for training.")
    parser.add_argument('--skip_model_save', action='store_true')
    parser.add_argument('--save_model_every_checkpoint', action='store_true')
    args = parser.parse_args()

    # If we ever want to implement checkpointing, just persist these values
    # every once in a while, and then load them from disk here.
    start_step = 0
    algorithm_dict = None

    os.makedirs(args.output_dir, exist_ok=True)
    sys.stdout = misc.Tee(os.path.join(args.output_dir, 'out.txt'))
    sys.stderr = misc.Tee(os.path.join(args.output_dir, 'err.txt'))

    print("Environment:")
    print("\tPython: {}".format(sys.version.split(" ")[0]))
    print("\tPyTorch: {}".format(torch.__version__))
    print("\tTorchvision: {}".format(torchvision.__version__))
    print("\tCUDA: {}".format(torch.version.cuda))
    print("\tCUDNN: {}".format(torch.backends.cudnn.version()))
    print("\tNumPy: {}".format(np.__version__))
    print("\tPIL: {}".format(PIL.__version__))

    print('Args:')
    for k, v in sorted(vars(args).items()):
        print('\t{}: {}'.format(k, v))

    if args.hparams_seed == 0:
        hparams = hparams_registry.default_hparams(args.algorithm, args.dataset)
    else:
        hparams = hparams_registry.random_hparams(args.algorithm, args.dataset,
            misc.seed_hash(args.hparams_seed, args.trial_seed))
    if args.hparams:
        hparams.update(json.loads(args.hparams))

    print('HParams:')
    for k, v in sorted(hparams.items()):
        print('\t{}: {}'.format(k, v))

    random.seed(args.seed)
    np.random.seed(args.seed)
    torch.manual_seed(args.seed)
    torch.backends.cudnn.deterministic = True
    torch.backends.cudnn.benchmark = False

    if torch.cuda.is_available():
        device = "cuda"
    else:
        device = "cpu"

    if args.dataset in vars(datasets):
        dataset = vars(datasets)[args.dataset](args.data_dir,
            args.test_envs, hparams)
    else:
        raise NotImplementedError

    # Split each env into an 'in-split' and an 'out-split'. We'll train on
    # each in-split except the test envs, and evaluate on all splits.

    # To allow unsupervised domain adaptation experiments, we split each test
    # env into 'in-split', 'uda-split' and 'out-split'. The 'in-split' is used
    # by collect_results.py to compute classification accuracies.  The
    # 'out-split' is used by the Oracle model selectino method. The unlabeled
    # samples in 'uda-split' are passed to the algorithm at training time if
    # args.task == "domain_adaptation". If we are interested in comparing
    # domain generalization and domain adaptation results, then domain
    # generalization algorithms should create the same 'uda-splits', which will
    # be discared at training.
    in_splits = []
    out_splits = []
    uda_splits = []
    for env_i, env in enumerate(dataset):
        uda = []

        out, in_ = misc.split_dataset(env,
            int(len(env)*args.holdout_fraction),
            misc.seed_hash(args.trial_seed, env_i))

        if env_i in args.test_envs:
            uda, in_ = misc.split_dataset(in_,
                int(len(in_)*args.uda_holdout_fraction),
                misc.seed_hash(args.trial_seed, env_i))

        if hparams['class_balanced']:
            in_weights = misc.make_weights_for_balanced_classes(in_)
            out_weights = misc.make_weights_for_balanced_classes(out)
            if uda is not None:
                uda_weights = misc.make_weights_for_balanced_classes(uda)
        else:
            in_weights, out_weights, uda_weights = None, None, None
        in_splits.append((in_, in_weights))
        out_splits.append((out, out_weights))
        if len(uda):
            uda_splits.append((uda, uda_weights))

    if args.task == "domain_adaptation" and len(uda_splits) == 0:
        raise ValueError("Not enough unlabeled samples for domain adaptation.")

    train_loaders = [InfiniteDataLoader(
        dataset=env,
        weights=env_weights,
        batch_size=hparams['batch_size'],
        num_workers=dataset.N_WORKERS)
        for i, (env, env_weights) in enumerate(in_splits)
        if i not in args.test_envs]

    uda_loaders = [InfiniteDataLoader(
        dataset=env,
        weights=env_weights,
        batch_size=hparams['batch_size'],
        num_workers=dataset.N_WORKERS)
        for i, (env, env_weights) in enumerate(uda_splits)
        # if i in args.test_envs
    ]

    ood_loader = [InfiniteDataLoader(
        dataset=env,
        weights=None,
        batch_size=hparams['batch_size'],
        num_workers=dataset.N_WORKERS)
        for i, (env, env_weights) in enumerate(uda_splits)
    ]

    eval_loaders = [FastDataLoader(
        dataset=env,
        batch_size=64,
        num_workers=dataset.N_WORKERS)
        for env, _ in (in_splits + out_splits + uda_splits)]
    eval_weights = [None for _, weights in (in_splits + out_splits + uda_splits)]
    eval_loader_names = ['env{}_in'.format(i)
        for i in range(len(in_splits))]
    eval_loader_names += ['env{}_out'.format(i)
        for i in range(len(out_splits))]
    eval_loader_names += ['env{}_uda'.format(i)
        for i in range(len(uda_splits))]

    algorithm_class = algorithms.get_algorithm_class(args.algorithm)
    algorithm = algorithm_class(dataset.input_shape, dataset.num_classes,
        len(dataset) - len(args.test_envs), hparams)

    if algorithm_dict is not None:
        algorithm.load_state_dict(algorithm_dict)

    algorithm.to(device)

    train_minibatches_iterator = zip(*train_loaders)
    uda_minibatches_iterator = zip(*uda_loaders)
    checkpoint_vals = collections.defaultdict(lambda: [])

    steps_per_epoch = min([len(env)/hparams['batch_size'] for env,_ in in_splits])

    n_steps = args.steps or dataset.N_STEPS
    checkpoint_freq = args.checkpoint_freq or dataset.CHECKPOINT_FREQ

    distilled_model = None

    def save_checkpoint(filename):
        if args.skip_model_save:
            return
        save_dict = {
            "args": vars(args),
            "model_input_shape": dataset.input_shape,
            "model_num_classes": dataset.num_classes,
            "model_num_domains": len(dataset) - len(args.test_envs),
            "model_hparams": hparams,
            "model_dict": algorithm.state_dict(),
            "distilled_model": None if distilled_model is None else distilled_model.state_dict()
        }
        torch.save(save_dict, os.path.join(args.output_dir, filename))


    last_results_keys = None
    for step in range(start_step, n_steps):
        step_start_time = time.time()
        minibatches_device = [(x.to(device), y.to(device))
            for x,y in next(train_minibatches_iterator)]
        if args.task == "domain_adaptation":
            uda_device = [x.to(device)
                for x,_ in next(uda_minibatches_iterator)]
        else:
            uda_device = None
        step_vals = algorithm.update(minibatches_device, uda_device)
        checkpoint_vals['step_time'].append(time.time() - step_start_time)

        for key, val in step_vals.items():
            checkpoint_vals[key].append(val)

        if (step % checkpoint_freq == 0) or (step == n_steps - 1):
            results = {
                'step': step,
                'epoch': step / steps_per_epoch,
            }

            for key, val in checkpoint_vals.items():
                results[key] = np.mean(val)

            evals = zip(eval_loader_names, eval_loaders, eval_weights)
            for name, loader, weights in evals:
                acc = misc.accuracy(algorithm, loader, weights, device)
                results[name+'_acc'] = acc

            if device == 'cuda':
                results['mem_gb'] = torch.cuda.max_memory_allocated() / (1024.*1024.*1024.)
            else:
                results['mem_gb'] = 0.

            results_keys = sorted(results.keys())
            if results_keys != last_results_keys:
                misc.print_row(results_keys, colwidth=12)
                last_results_keys = results_keys
            misc.print_row([results[key] for key in results_keys],
                colwidth=12)

            results.update({
                'hparams': hparams,
                'args': vars(args)
            })

            epochs_path = os.path.join(args.output_dir, 'results.jsonl')
            with open(epochs_path, 'a') as f:
                f.write(json.dumps(results, sort_keys=True) + "\n")

            algorithm_dict = algorithm.state_dict()
            start_step = step + 1
            checkpoint_vals = collections.defaultdict(lambda: [])

            if args.save_model_every_checkpoint:
                save_checkpoint(f'model_step{step}.pkl')

    # Use the model to find the most confident samples in the test environment
    self_distill(hparams, dataset.input_shape, dataset.num_classes, algorithm.network, train_loaders, uda_loaders, n_steps, hparams['lr'] * 0.01)


    num_conf_iters = hparams['num_confidence_runs']
    prediction_history = []

    # get batch of test data
    data_batch = [x for x, _ in next(uda_minibatches_iterator)]

    for i in range(prediction_history):
        prediction = algorithm.predict(torch.vstack(data_batch))
        pass

    # Use the most confident samples to train an ERM model


    save_checkpoint('model.pkl')

    with open(os.path.join(args.output_dir, 'done'), 'w') as f:
        f.write('done')
