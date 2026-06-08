#!/usr/bin/env python3
"""Sample N random instance IDs from SWE-bench Verified (or other subset).

Uses a fixed random seed for reproducibility. Prints the sampled IDs to
stdout, one per line, in the order they appear in the dataset (not the
random draw order, so that downstream filters/lookups are deterministic).

Usage:
    python scripts/sample_instances.py --subset verified --split test \
        --n 5 --seed 1
"""
from __future__ import annotations

import argparse
import os
import random
import sys
from pathlib import Path

# Repo-root on path so this script can be run directly
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--subset", default="verified",
                   choices=["verified", "lite", "full", "multimodal", "multilingual"])
    p.add_argument("--split", default="test")
    p.add_argument("--n", type=int, required=True, help="number of instances to sample")
    p.add_argument("--seed", type=int, required=True, help="random seed for reproducibility")
    p.add_argument("--dataset", default=None,
                   help="override HF dataset name (default: princeton-nlp/SWE-bench_<subset>)")
    p.add_argument("--shuffle", action="store_true",
                   help="randomize the output order (default: keep dataset order)")
    args = p.parse_args()

    if args.dataset is None:
        from minisweagent.run.benchmarks.swebench import DATASET_MAPPING
        dataset_path = DATASET_MAPPING.get(args.subset, args.subset)
    else:
        dataset_path = args.dataset

    from datasets import load_dataset
    ds = load_dataset(dataset_path, split=args.split)
    all_ids = [d["instance_id"] for d in ds]
    n_total = len(all_ids)
    if args.n > n_total:
        print(f"✗ --n {args.n} > dataset size {n_total}", file=sys.stderr)
        return 1

    # Reproducible sampling
    rng = random.Random(args.seed)
    picked = rng.sample(range(n_total), args.n)
    sampled_ids = [all_ids[i] for i in picked]

    if not args.shuffle:
        # sort by original index so output is in dataset order
        sampled_ids = sorted(sampled_ids, key=lambda iid: all_ids.index(iid))

    for iid in sampled_ids:
        print(iid)
    return 0


if __name__ == "__main__":
    sys.exit(main())
