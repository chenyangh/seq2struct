import argparse
import collections
import datetime
import json
import os

import _jsonnet
import attr
import asdl
import torch

from seq2struct import ast_util
from seq2struct.utils import registry
from seq2struct.utils import saver as saver_mod
from seq2struct.utils import vocab


@attr.s
class TrainConfig:
    eval_every_n = attr.ib(default=100)
    report_every_n = attr.ib(default=100)
    save_every_n = attr.ib(default=100)
    keep_every_n = attr.ib(default=1000)

    batch_size = attr.ib(default=32)
    eval_batch_size = attr.ib(default=32)
    max_steps = attr.ib(default=100000)
    num_eval_items = attr.ib(default=None)


def log(msg):
    print('[{}] {}'.format(
        datetime.datetime.now().replace(microsecond=0).isoformat(),
        msg))


def eval_model(model, last_step, eval_data_loader, eval_section, num_eval_items=None):
    stats = collections.defaultdict(float)
    model.eval()
    for eval_batch in eval_data_loader:
        batch_res = model.eval_on_batch(eval_batch)
        for k, v in batch_res.items():
            stats[k] += v
        if num_eval_items and stats['total'] > num_eval_items:
            break
    model.train()

    # Divide each stat by 'total'
    for k in stats:
        stats[k] /= stats['total']
    del stats['total']

    log("Step {} stats, {}: {}".format(
        last_step, eval_section, ", ".join(
        "{} = {}".format(k, v) for k, v in stats.items())))


def yield_batches_from_epochs(loader):
    while True:
        for batch in loader:
            yield batch


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--logdir', required=True)
    parser.add_argument('--config', required=True)
    args = parser.parse_args()

    if torch.cuda.is_available():
        device = torch.device('cuda')
    else:
        device = torch.device('cpu')
    config = json.loads(_jsonnet.evaluate_file(args.config))
    train_config = registry.instantiate(TrainConfig, config['train'])

    # 0. Construct preprocessors
    model_preproc = registry.instantiate(
        registry.lookup('model', config['model']).Preproc,
        config['model'])
    model_preproc.load()

    # 1. Construct model
    model = registry.construct('model', config['model'], preproc=model_preproc, device=device)
    model.to(device)

    optimizer = registry.construct('optimizer', config['optimizer'], params=model.parameters())

    # 2. Restore its parameters
    saver = saver_mod.Saver(
        model, optimizer, keep_every_n=train_config.keep_every_n)
    last_step = saver.restore(args.logdir)

    # 3. Get training data somewhere
    train_data = model_preproc.dataset('train')
    train_data_loader = yield_batches_from_epochs(
        torch.utils.data.DataLoader(
            train_data,
            batch_size=train_config.batch_size,
            shuffle=True,
            drop_last=True,
            collate_fn=lambda x: x))
    train_eval_data_loader = torch.utils.data.DataLoader(
            train_data,
            batch_size=train_config.eval_batch_size,
            collate_fn=lambda x: x)

    val_data = model_preproc.dataset('val')
    val_data_loader = torch.utils.data.DataLoader(
            val_data,
            batch_size=train_config.eval_batch_size,
            collate_fn=lambda x: x)

    # 4. Start training loop
    for batch in train_data_loader:
        # Quit if too long
        if last_step >= train_config.max_steps:
            break

        # Evaluate model
        if last_step % train_config.eval_every_n == 0:
            eval_model(model, last_step, train_eval_data_loader, 'train', num_eval_items=train_config.num_eval_items)
            eval_model(model, last_step, val_data_loader, 'val', num_eval_items=train_config.num_eval_items)

        # Compute and apply gradient
        # TODO: update learning rate
        optimizer.zero_grad()
        loss = model.compute_loss(batch)
        loss.backward()
        optimizer.step()

        # Report metrics
        if last_step % train_config.report_every_n == 0:
            log('Step {}: loss={:.4f}'.format(last_step, loss.item()))

        last_step += 1
        # Run saver
        if last_step % train_config.save_every_n == 0:
            saver.save(args.logdir, last_step)


if __name__ == '__main__':
    main()