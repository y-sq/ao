# Copyright (c) Meta Platforms, Inc. and affiliates.

import os
import sys
import warnings
import hashlib

import presets
import torch
import torch.utils.data
import torchvision
import utils
from torch import nn
from torchvision.transforms.functional import InterpolationMode

sys.path.append(os.path.join(os.path.dirname(__file__), ".."))
from supermask import apply_supermask, SupermaskLinear
from benchmark import apply_sparsity, apply_bsr, verify_sparsity
from train import evaluate, _get_cache_path, load_data
from torchao.sparsity import sparsify_, apply_fake_sparsity, int8_dynamic_activation_int8_semi_sparse_weight, semi_sparse_weight
from torchao.sparsity import sparsify_, apply_fake_sparsity, int8_dynamic_activation_int8_semi_sparse_weight, semi_sparse_weight

def main(args):
    utils.init_distributed_mode(args)
    print(args)

    device = torch.device(args.device)
    # We disable the cudnn benchmarking because it can noticeably affect the accuracy
    torch.backends.cudnn.benchmark = False
    torch.backends.cudnn.deterministic = True

    # Load validation data
    val_dir = os.path.join(args.data_path, "val")
    dataset_test, test_sampler = load_data(None, val_dir, args)
    data_loader_test = torch.utils.data.DataLoader(
        dataset_test, batch_size=args.batch_size, sampler=test_sampler, num_workers=args.workers, pin_memory=True
    )
    num_classes = len(dataset_test.classes)

    # Create Model
    print("Creating model")
    model = torchvision.models.get_model(args.model, weights=args.weights, num_classes=num_classes)

    if args.sparsity == "bsr":
        apply_supermask(
            model,
            linear_sparsity=args.sparsity_linear,
            linear_sp_tilesize=args.sp_linear_tile_size,
            conv1x1_sparsity=args.sparsity_conv1x1,
            conv1x1_sp_tilesize=args.sp_conv1x1_tile_size,
            conv_sparsity=args.sparsity_conv,
            conv_sp_tilesize=args.sp_conv_tile_size,
            skip_last_layer_sparsity=args.skip_last_layer_sparsity,
            skip_first_transformer_sparsity=args.skip_first_transformer_sparsity,
            device=device,
            verbose=False,
        )

    sparsifier = None
    if args.sparsity == "semi_structured":
        sparse_config = []
        from torch.ao.pruning import WeightNormSparsifier
        for name, mod in model.named_modules():
            if args.skip_last_layer_sparsity and "heads.head" in name:
                continue
            if args.skip_first_transformer_sparsity and "encoder.layers.encoder_layer_0" in name:
                continue
            if isinstance(mod, torch.nn.Linear) and "mlp" in name: 
                sparse_config.append({"tensor_fqn": f"{name}.weight"})

        sparsifier = WeightNormSparsifier(
            sparsity_level=1.0, sparse_block_shape=(1, 4), zeros_per_block=2
        )
        sparsifier.prepare(model, sparse_config)
        for line in sparse_config:
            print(line)
        sparsifier.step()
        if not args.weights_path:
            sparsifier.squash_mask()

    if args.weights_path:
        try:
            checkpoint = torch.load(args.weights_path, map_location="cpu")
            model.load_state_dict(checkpoint["model"])
            print(f"Loaded checkpoint successfully from: {args.weights_path}")
        except FileNotFoundError:
            raise FileNotFoundError(f"No checkpoint found at {args.weights_path}")

    model.to(device).bfloat16()

    if args.sparsity == "bsr":
        assert args.bsr is not None and args.bsr > 0
        apply_sparsity(model)
        verify_sparsity(model)
        if args.bsr:
            apply_bsr(model, args.bsr)

    if sparsifier:
        sparsifier.squash_mask()
    if args.sparsity == "semi_structured":
        torch.sparse.SparseSemiStructuredTensor._FORCE_CUTLASS = False
        def filter_fn(mod, name):
            if args.skip_last_layer_sparsity and "heads.head" in name:
                return False
            if args.skip_first_transformer_sparsity and "encoder.layers.encoder_layer_0" in name:
                return False
            if isinstance(mod, torch.nn.Linear) and "mlp" in name: 
                return True
            return False
        sparsify_(model, semi_sparse_weight(), filter_fn)
            
    criterion = nn.CrossEntropyLoss(label_smoothing=args.label_smoothing)
    evaluate(model, criterion, data_loader_test, device=device, dtype=torch.bfloat16)


def get_args_parser(add_help=True):
    import argparse

    parser = argparse.ArgumentParser(description="Superblock evaluation", add_help=add_help)
    parser.add_argument("--data-path", default="/datasets01/imagenet_full_size/061417", type=str, help="dataset path")
    parser.add_argument("--model", default="vit-", type=str, help="model name")
    parser.add_argument("--device", default="cuda", type=str, help="device (Use cuda or cpu Default: cuda)")
    parser.add_argument(
        "-b", "--batch-size", default=256, type=int, help="images per gpu, the total batch size is $NGPU x batch_size"
    )
    parser.add_argument(
        "-j", "--workers", default=16, type=int, metavar="N", help="number of data loading workers (default: 16)"
    )
    parser.add_argument(
        "--label-smoothing", default=0.0, type=float, help="label smoothing (default: 0.0)", dest="label_smoothing"
    )
    parser.add_argument("--print-freq", default=10, type=int, help="print frequency")
    parser.add_argument(
        "--cache-dataset",
        dest="cache_dataset",
        help="Cache the datasets for quicker initialization. It also serializes the transforms",
        action="store_true",
    )
    parser.add_argument(
        "--interpolation", default="bilinear", type=str, help="the interpolation method (default: bilinear)"
    )
    parser.add_argument(
        "--val-resize-size", default=256, type=int, help="the resize size used for validation (default: 256)"
    )
    parser.add_argument(
        "--val-crop-size", default=224, type=int, help="the central crop size used for validation (default: 224)"
    )
    parser.add_argument("--weights", default=None, type=str, help="the weights enum name to load")
    parser.add_argument("--weights-path", type=str, help="path of pretrained weights to load")

    # NOTE: sparsity args
    parser.add_argument("--sparsity", choices=["bsr", "semi_structured"], default=None, help='weight sparsification to apply')
    parser.add_argument("--sparsity-linear", type=float, default=0.0)
    parser.add_argument("--sp-linear-tile-size", type=int, default=1)
    parser.add_argument("--sparsity-conv1x1", type=float, default=0.0)
    parser.add_argument("--sp-conv1x1-tile-size", type=int, default=1)
    parser.add_argument("--sparsity-conv", type=float, default=0.0)
    parser.add_argument("--sp-conv-tile-size", type=int, default=1)
    parser.add_argument("--skip-last-layer-sparsity", action="store_true", help="Skip applying sparsity to the last linear layer (for vit only)")
    parser.add_argument("--skip-first-transformer-sparsity", action="store_true", help="Skip applying sparsity to the first transformer layer (for vit only)")
    parser.add_argument('--bsr', type=int, nargs='?', default=64, help='Convert sparsified weights to BSR format with optional block size (default: 64)')
    parser.add_argument('--meta', action='store_true', help='Use Meta internal imagenet structure')

    return parser


if __name__ == "__main__":
    args = get_args_parser().parse_args()
    main(args)