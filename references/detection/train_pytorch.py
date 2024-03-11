# Copyright (C) 2021-2024, Mindee.

# This program is licensed under the Apache License 2.0.
# See LICENSE or go to <https://opensource.org/licenses/Apache-2.0> for full license details.

import os
from pathlib import Path
from doctr.file_utils import CLASS_NAME
from doctr import datasets


os.environ["USE_TORCH"] = "1"

import datetime
import hashlib
import logging
import multiprocessing as mp
import time
from pathlib import Path

import numpy as np
import psutil
import torch
import wandb
from torch.optim.lr_scheduler import CosineAnnealingLR, MultiplicativeLR, OneCycleLR, PolynomialLR
from torch.utils.data import DataLoader, RandomSampler, SequentialSampler
from torchvision.transforms.v2 import Compose, GaussianBlur, Normalize, RandomGrayscale, RandomPhotometricDistort
from tqdm.auto import tqdm

from doctr import transforms as T
from doctr.datasets import DetectionDataset
from doctr.models import detection, login_to_hub, push_to_hf_hub
from doctr.utils.metrics import LocalizationConfusion
from utils import EarlyStopper, plot_recorder, plot_samples

SLACK_WEBHOOK_URL = None
SLACK_WEBHOOK_PATH = Path(os.path.join(os.path.expanduser("~"), ".config", "doctr", "slack_webhook_url.txt"))
if SLACK_WEBHOOK_PATH.exists():
    with open(SLACK_WEBHOOK_PATH) as f:
        SLACK_WEBHOOK_URL = f.read().strip()
else:
    print(f"{SLACK_WEBHOOK_PATH} does not exist, skip Slack integration configuration...")


def send_on_slack(text: str):
    """Send a message on Slack.

    Args:
        text (str): message to send on Slack
    """
    if SLACK_WEBHOOK_URL:
        try:
            import requests

            requests.post(
                url=SLACK_WEBHOOK_URL,
                json={"text": f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}]: {text}"},
            )
        except Exception:
            print("Impossible to send message on Slack, continue...")


def record_lr(
    model: torch.nn.Module,
    train_loader: DataLoader,
    batch_transforms,
    optimizer,
    start_lr: float = 1e-7,
    end_lr: float = 1,
    num_it: int = 100,
    amp: bool = False,
):
    """Gridsearch the optimal learning rate for the training.
    Adapted from https://github.com/frgfm/Holocron/blob/master/holocron/trainer/core.py
    """
    if num_it > len(train_loader):
        raise ValueError("the value of `num_it` needs to be lower than the number of available batches")

    model = model.train()
    # Update param groups & LR
    optimizer.defaults["lr"] = start_lr
    for pgroup in optimizer.param_groups:
        pgroup["lr"] = start_lr

    gamma = (end_lr / start_lr) ** (1 / (num_it - 1))
    scheduler = MultiplicativeLR(optimizer, lambda step: gamma)

    lr_recorder = [start_lr * gamma**idx for idx in range(num_it)]
    loss_recorder = []

    if amp:
        scaler = torch.cuda.amp.GradScaler()

    for batch_idx, (images, targets) in enumerate(train_loader):
        if torch.cuda.is_available():
            images = images.cuda()

        images = batch_transforms(images)

        # Forward, Backward & update
        optimizer.zero_grad()
        if amp:
            with torch.cuda.amp.autocast():
                train_loss = model(images, targets)["loss"]
            scaler.scale(train_loss).backward()
            # Gradient clipping
            scaler.unscale_(optimizer)
            torch.nn.utils.clip_grad_norm_(model.parameters(), 5)
            # Update the params
            scaler.step(optimizer)
            scaler.update()
        else:
            train_loss = model(images, targets)["loss"]
            train_loss.backward()
            torch.nn.utils.clip_grad_norm_(model.parameters(), 5)
            optimizer.step()
        # Update LR
        scheduler.step()

        # Record
        if not torch.isfinite(train_loss):
            if batch_idx == 0:
                raise ValueError("loss value is NaN or inf.")
            else:
                break
        loss_recorder.append(train_loss.item())
        # Stop after the number of iterations
        if batch_idx + 1 == num_it:
            break

    return lr_recorder[: len(loss_recorder)], loss_recorder


def fit_one_epoch(model, train_loader, batch_transforms, optimizer, scheduler, amp=False):
    if amp:
        scaler = torch.cuda.amp.GradScaler()

    model.train()
    # Iterate over the batches of the dataset
    last_progress = 0
    interval_progress = 5
    pbar = tqdm(train_loader, position=1)
    send_on_slack(str(pbar))
    for images, targets in pbar:
        if torch.cuda.is_available():
            images = images.cuda()
        images = batch_transforms(images)

        optimizer.zero_grad()
        if amp:
            with torch.cuda.amp.autocast():
                train_loss = model(images, targets)["loss"]
            scaler.scale(train_loss).backward()
            # Gradient clipping
            scaler.unscale_(optimizer)
            torch.nn.utils.clip_grad_norm_(model.parameters(), 5)
            # Update the params
            scaler.step(optimizer)
            scaler.update()
        else:
            train_loss = model(images, targets)["loss"]
            train_loss.backward()
            torch.nn.utils.clip_grad_norm_(model.parameters(), 5)
            optimizer.step()

        scheduler.step()
        pbar.set_description(f"Training loss: {train_loss.item():.6}")
        current_progress = pbar.n / pbar.total * 100
        if current_progress - last_progress > interval_progress:
            send_on_slack(str(pbar))
            last_progress = int(current_progress)
    send_on_slack(f"Final training loss: {train_loss.item():.6}")


@torch.no_grad()
def evaluate(model, val_loader, batch_transforms, val_metric, amp=False):
    # Model in eval mode
    model.eval()
    # Reset val metric
    val_metric.reset()
    last_progress = 0
    interval_progress = 5
    pbar = tqdm(val_loader)
    send_on_slack(str(pbar))
    # Validation loop
    val_loss, batch_cnt = 0, 0
    for images, targets in pbar:
        if torch.cuda.is_available():
            images = images.cuda()
        images = batch_transforms(images)
        if amp:
            with torch.cuda.amp.autocast():
                out = model(images, targets, return_preds=True)
        else:
            out = model(images, targets, return_preds=True)
        # Compute metric
        loc_preds = out["preds"]
        for target, loc_pred in zip(targets, loc_preds):
            for boxes_gt, boxes_pred in zip(target.values(), loc_pred.values()):
                if args.rotation and args.eval_straight:
                    # Convert pred to boxes [xmin, ymin, xmax, ymax]  N, 4, 2 --> N, 4
                    boxes_pred = np.concatenate((boxes_pred.min(axis=1), boxes_pred.max(axis=1)), axis=-1)
                val_metric.update(gts=boxes_gt, preds=boxes_pred[:, :4])

        current_progress = pbar.n / pbar.total * 100
        if current_progress - last_progress > interval_progress:
            send_on_slack(str(pbar))
            last_progress = int(current_progress)
        val_loss += out["loss"].item()
        batch_cnt += 1

    val_loss /= batch_cnt
    recall, precision, mean_iou = val_metric.summary()
    return val_loss, recall, precision, mean_iou


def main(args):
    print(args)
    send_on_slack(f"Start training: {args}")

    if args.push_to_hub:
        login_to_hub()

    if not isinstance(args.workers, int):
        args.workers = min(16, mp.cpu_count())

    torch.backends.cudnn.benchmark = True
    system_available_memory = int(psutil.virtual_memory().available / 1024**3)

    st = time.time()
    val_set = DetectionDataset(
        img_folder=os.path.join(args.val_path, "images"),
        label_path=os.path.join(args.val_path, "labels.json"),
        sample_transforms=T.SampleCompose(
            (
                [T.Resize((args.input_size, args.input_size), preserve_aspect_ratio=True, symmetric_pad=True)]
                if not args.rotation or args.eval_straight
                else []
            )
            + (
                [
                    T.Resize(args.input_size, preserve_aspect_ratio=True),  # This does not pad
                    T.RandomApply(T.RandomRotate(90, expand=True), 0.5),
                    T.Resize((args.input_size, args.input_size), preserve_aspect_ratio=True, symmetric_pad=True),
                ]
                if args.rotation and not args.eval_straight
                else []
            )
        ),
        use_polygons=args.rotation and not args.eval_straight,
    )
    val_loader = DataLoader(
        val_set,
        batch_size=args.batch_size,
        drop_last=False,
        num_workers=args.workers,
        sampler=SequentialSampler(val_set),
        pin_memory=torch.cuda.is_available(),
        collate_fn=val_set.collate_fn,
    )
    print(f"Validation set loaded in {time.time() - st:.4}s ({len(val_set)} samples in " f"{len(val_loader)} batches)")
    send_on_slack(
        f"Validation set loaded in {time.time() - st:.4}s ({len(val_set)} samples in " f"{len(val_loader)} batches)"
    )
    with open(os.path.join(args.val_path, "labels.json"), "rb") as f:
        val_hash = hashlib.sha256(f.read()).hexdigest()

    batch_transforms = Normalize(mean=(0.798, 0.785, 0.772), std=(0.264, 0.2749, 0.287))

    funsd_ds = DetectionDataset(
        img_folder=os.path.join(args.funsd_path, "images"),
        label_path=os.path.join(args.funsd_path, "labels.json"),
        sample_transforms=T.SampleCompose(
            (
                [T.Resize((args.input_size, args.input_size), preserve_aspect_ratio=True, symmetric_pad=True)]
                if not args.rotation or args.eval_straight
                else []
            )
            + (
                [
                    T.Resize(args.input_size, preserve_aspect_ratio=True),  # This does not pad
                    T.RandomApply(T.RandomRotate(90, expand=True), 0.5),
                    T.Resize((args.input_size, args.input_size), preserve_aspect_ratio=True, symmetric_pad=True),
                ]
                if args.rotation and not args.eval_straight
                else []
            )
        ),
        use_polygons=args.rotation and not args.eval_straight,
    )

    funsd_test_loader = DataLoader(
        funsd_ds,
        batch_size=args.batch_size,
        drop_last=False,
        num_workers=args.workers,
        sampler=SequentialSampler(funsd_ds),
        pin_memory=torch.cuda.is_available(),
        collate_fn=funsd_ds.collate_fn,
    )
    print(f"FUNSD Test set loaded in {time.time() - st:.4}s ({len(funsd_ds)} samples in " f"{len(funsd_test_loader)} batches)")


    cord_ds = DetectionDataset(
        img_folder=os.path.join(args.cord_path, "images"),
        label_path=os.path.join(args.cord_path, "labels.json"),
        sample_transforms=T.SampleCompose(
            (
                [T.Resize((args.input_size, args.input_size), preserve_aspect_ratio=True, symmetric_pad=True)]
                if not args.rotation or args.eval_straight
                else []
            )
            + (
                [
                    T.Resize(args.input_size, preserve_aspect_ratio=True),  # This does not pad
                    T.RandomApply(T.RandomRotate(90, expand=True), 0.5),
                    T.Resize((args.input_size, args.input_size), preserve_aspect_ratio=True, symmetric_pad=True),
                ]
                if args.rotation and not args.eval_straight
                else []
            )
        ),
        use_polygons=args.rotation and not args.eval_straight,
    )

    cord_test_loader = DataLoader(
        cord_ds,
        batch_size=args.batch_size,
        drop_last=False,
        num_workers=args.workers,
        sampler=SequentialSampler(cord_ds),
        pin_memory=torch.cuda.is_available(),
        collate_fn=cord_ds.collate_fn,
    )
    print(f"CORD Test set loaded in {time.time() - st:.4}s ({len(cord_ds)} samples in " f"{len(funsd_test_loader)} batches)")

    # Load doctr model
    model = detection.__dict__[args.arch](
        pretrained=args.pretrained,
        assume_straight_pages=not args.rotation,
        class_names=val_set.class_names,
    )

    # Resume weights
    if isinstance(args.resume, str):
        print(f"Resuming {args.resume}")
        send_on_slack(f"Resuming {args.resume}")
        checkpoint = torch.load(args.resume, map_location="cpu")
        model.load_state_dict(checkpoint)

    # GPU
    if isinstance(args.device, int):
        if not torch.cuda.is_available():
            raise AssertionError("PyTorch cannot access your GPU. Please investigate!")
        if args.device >= torch.cuda.device_count():
            raise ValueError("Invalid device index")
    # Silent default switch to GPU if available
    elif torch.cuda.is_available():
        args.device = 0
    else:
        logging.warning("No accessible GPU, targe device set to CPU.")
    if torch.cuda.is_available():
        torch.cuda.set_device(args.device)
        model = model.cuda()

    # Metrics
    val_metric = LocalizationConfusion(
        use_polygons=args.rotation and not args.eval_straight,
        mask_shape=(args.input_size, args.input_size),
        use_broadcasting=True if system_available_memory > 62 else False,
    )
    funsd_val_metric = LocalizationConfusion(
        use_polygons=args.rotation and not args.eval_straight,
        mask_shape=(args.input_size, args.input_size),
        use_broadcasting=True if system_available_memory > 62 else False,
    )
    cord_val_metric = LocalizationConfusion(
        use_polygons=args.rotation and not args.eval_straight,
        mask_shape=(args.input_size, args.input_size),
        use_broadcasting=True if system_available_memory > 62 else False,
    )

    if args.test_only:
        print("Running evaluation")
        val_loss, recall, precision, mean_iou = evaluate(model, val_loader, batch_transforms, val_metric, amp=args.amp)
        print(
            f"Validation loss: {val_loss:.6} (Recall: {recall:.2%} | Precision: {precision:.2%} | "
            f"Mean IoU: {mean_iou:.2%})"
        )
        return

    st = time.time()
    # Load both train and val data generators
    train_set = DetectionDataset(
        img_folder=os.path.join(args.train_path, "images"),
        label_path=os.path.join(args.train_path, "labels.json"),
        img_transforms=Compose(
            [
                # Augmentations
                T.RandomApply(T.ColorInversion(), 0.1),
                T.RandomApply(T.GaussianNoise(mean=0.1, std=0.1), 0.1),
                T.RandomApply(T.RandomShadow(), 0.1),
                T.RandomApply(GaussianBlur(kernel_size=3), 0.1),
                RandomPhotometricDistort(p=0.05),
                RandomGrayscale(p=0.05),
            ]
        ),
        sample_transforms=T.SampleCompose(
            (
                [T.Resize((args.input_size, args.input_size), preserve_aspect_ratio=True, symmetric_pad=True)]
                if not args.rotation
                else []
            )
            + (
                [
                    T.Resize(args.input_size, preserve_aspect_ratio=True),
                    T.RandomApply(T.RandomRotate(90, expand=True), 0.5),
                    T.Resize((args.input_size, args.input_size), preserve_aspect_ratio=True, symmetric_pad=True),
                ]
                if args.rotation
                else []
            )
        ),
        use_polygons=args.rotation,
    )

    train_loader = DataLoader(
        train_set,
        batch_size=args.batch_size,
        drop_last=True,
        num_workers=args.workers,
        sampler=RandomSampler(train_set),
        pin_memory=torch.cuda.is_available(),
        collate_fn=train_set.collate_fn,
    )
    print(f"Train set loaded in {time.time() - st:.4}s ({len(train_set)} samples in " f"{len(train_loader)} batches)")
    send_on_slack(
        f"Train set loaded in {time.time() - st:.4}s ({len(train_set)} samples in " f"{len(train_loader)} batches)"
    )
    with open(os.path.join(args.train_path, "labels.json"), "rb") as f:
        train_hash = hashlib.sha256(f.read()).hexdigest()

    if args.show_samples:
        x, target = next(iter(train_loader))
        plot_samples(x, target)
        return

    # Backbone freezing
    if args.freeze_backbone:
        for p in model.feat_extractor.parameters():
            p.requires_grad = False

    # Optimizer
    optimizer = torch.optim.Adam(
        [p for p in model.parameters() if p.requires_grad],
        args.lr,
        betas=(0.95, 0.99),
        eps=1e-6,
        weight_decay=args.weight_decay,
    )
    # LR Finder
    if args.find_lr:
        lrs, losses = record_lr(model, train_loader, batch_transforms, optimizer, amp=args.amp)
        plot_recorder(lrs, losses)
        return
    # Scheduler
    if args.sched == "cosine":
        scheduler = CosineAnnealingLR(optimizer, args.epochs * len(train_loader), eta_min=args.lr / 25e4)
    elif args.sched == "onecycle":
        scheduler = OneCycleLR(optimizer, args.lr, args.epochs * len(train_loader))
    elif args.sched == "poly":
        scheduler = PolynomialLR(optimizer, args.epochs * len(train_loader))

    # Training monitoring
    current_time = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    exp_name = f"{args.arch}_{current_time}" if args.name is None else args.name

    # W&B
    if args.wb:
        run = wandb.init(
            name=exp_name,
            project="text-detection",
            config={
                "learning_rate": args.lr,
                "epochs": args.epochs,
                "weight_decay": args.weight_decay,
                "batch_size": args.batch_size,
                "architecture": args.arch,
                "input_size": args.input_size,
                "optimizer": "adam",
                "framework": "pytorch",
                "scheduler": args.sched,
                "train_hash": train_hash,
                "val_hash": val_hash,
                "pretrained": args.pretrained,
                "rotation": args.rotation,
                "amp": args.amp,
            },
        )

    # Create loss queue
    min_loss = np.inf
    if args.early_stop:
        early_stopper = EarlyStopper(patience=args.early_stop_epochs, min_delta=args.early_stop_delta)

    # Training loop
    for epoch in range(args.epochs):
        fit_one_epoch(model, train_loader, batch_transforms, optimizer, scheduler, amp=args.amp)
        # Validation loop at the end of each epoch
        val_loss, recall, precision, mean_iou = evaluate(model, val_loader, batch_transforms, val_metric, amp=args.amp)
        funsd_recall, funsd_precision, funsd_mean_iou = 0.0, 0.0, 0.0
        cord_recall, cord_precision, cord_mean_iou = 0.0, 0.0, 0.0
        try:
            _, funsd_recall, funsd_precision, funsd_mean_iou = evaluate(
                model, funsd_test_loader, batch_transforms, funsd_val_metric, amp=args.amp
            )
        except Exception:
            pass
        try:
            _, cord_recall, cord_precision, cord_mean_iou = evaluate(
                model, cord_test_loader, batch_transforms, cord_val_metric, amp=args.amp
            )
        except Exception:
            pass
        if val_loss < min_loss:
            print(f"Validation loss decreased {min_loss:.6} --> {val_loss:.6}: saving state...")
            send_on_slack(f"Validation loss decreased {min_loss:.6} --> {val_loss:.6}: saving state...")
            torch.save(model.state_dict(), f"./{exp_name}.pt")
            min_loss = val_loss
        if args.save_interval_epoch:
            print(f"Saving state at epoch: {epoch + 1}")
            torch.save(model.state_dict(), f"./{exp_name}_epoch{epoch + 1}.pt")
        log_msg = f"Epoch {epoch + 1}/{args.epochs} - Validation loss: {val_loss:.6} "
        if any(val is None for val in (recall, precision, mean_iou)):
            log_msg += "(Undefined metric value, caused by empty GTs or predictions)"
        else:
            log_msg += f"(Recall: {recall:.2%} | Precision: {precision:.2%} | Mean IoU: {mean_iou:.2%})\n"
            log_msg += f"FUNSD: Recall: {funsd_recall:.2%} | Precision: {funsd_precision:.2%} | Mean IoU: {funsd_mean_iou:.2%}\n"
            log_msg += f"CORD: Recall: {cord_recall:.2%} | Precision: {cord_precision:.2%} | Mean IoU: {cord_mean_iou:.2%}"
        print(log_msg)
        send_on_slack(log_msg)
        # W&B
        if args.wb:
            wandb.log(
                {
                    "val_loss": val_loss,
                    "recall": recall,
                    "precision": precision,
                    "mean_iou": mean_iou,
                }
            )
        if args.early_stop and early_stopper.early_stop(val_loss):
            print("Training halted early due to reaching patience limit.")
            break
    if args.wb:
        run.finish()

    if args.push_to_hub:
        push_to_hf_hub(model, exp_name, task="detection", run_config=args)


def parse_args():
    import argparse

    parser = argparse.ArgumentParser(
        description="DocTR training script for text detection (PyTorch)",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )

    parser.add_argument("train_path", type=str, help="path to training data folder")
    parser.add_argument("val_path", type=str, help="path to validation data folder")
    parser.add_argument("funsd_path", type=str, help="path to FUNSD data folder")
    parser.add_argument("cord_path", type=str, help="path to Cord data folder")
    parser.add_argument("arch", type=str, help="text-detection model to train")
    parser.add_argument("--name", type=str, default=None, help="Name of your training experiment")
    parser.add_argument("--epochs", type=int, default=10, help="number of epochs to train the model on")
    parser.add_argument("-b", "--batch_size", type=int, default=2, help="batch size for training")
    parser.add_argument("--device", default=None, type=int, help="device")
    parser.add_argument(
        "--save-interval-epoch", dest="save_interval_epoch", action="store_true", help="Save model every epoch"
    )
    parser.add_argument("--input_size", type=int, default=1024, help="model input size, H = W")
    parser.add_argument("--lr", type=float, default=0.001, help="learning rate for the optimizer (Adam)")
    parser.add_argument("--wd", "--weight-decay", default=0, type=float, help="weight decay", dest="weight_decay")
    parser.add_argument("-j", "--workers", type=int, default=None, help="number of workers used for dataloading")
    parser.add_argument("--resume", type=str, default=None, help="Path to your checkpoint")
    parser.add_argument("--test-only", dest="test_only", action="store_true", help="Run the validation loop")
    parser.add_argument(
        "--freeze-backbone", dest="freeze_backbone", action="store_true", help="freeze model backbone for fine-tuning"
    )
    parser.add_argument(
        "--show-samples", dest="show_samples", action="store_true", help="Display unormalized training samples"
    )
    parser.add_argument("--wb", dest="wb", action="store_true", help="Log to Weights & Biases")
    parser.add_argument("--push-to-hub", dest="push_to_hub", action="store_true", help="Push to Huggingface Hub")
    parser.add_argument(
        "--pretrained",
        dest="pretrained",
        action="store_true",
        help="Load pretrained parameters before starting the training",
    )
    parser.add_argument("--rotation", dest="rotation", action="store_true", help="train with rotated documents")
    parser.add_argument(
        "--eval-straight",
        action="store_true",
        help="metrics evaluation with straight boxes instead of polygons to save time + memory",
    )
    parser.add_argument(
        "--sched", type=str, default="poly", choices=["cosine", "onecycle", "poly"], help="scheduler to use"
    )
    parser.add_argument("--amp", dest="amp", help="Use Automatic Mixed Precision", action="store_true")
    parser.add_argument("--find-lr", action="store_true", help="Gridsearch the optimal LR")
    parser.add_argument("--early-stop", action="store_true", help="Enable early stopping")
    parser.add_argument("--early-stop-epochs", type=int, default=5, help="Patience for early stopping")
    parser.add_argument("--early-stop-delta", type=float, default=0.01, help="Minimum Delta for early stopping")
    args = parser.parse_args()

    return args


if __name__ == "__main__":
    args = parse_args()
    main(args)
