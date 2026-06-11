#!/usr/bin/env python3
"""Print instance IDs to stdout, either as a slice or random sample.

Three modes (mutually exclusive; --slice wins if both given):

  1. --slice M:N   — deterministic contiguous slice of the dataset
                      (Python slice semantics, N may be omitted for "to end")
  2. --input-file   — read a literal newline-delimited list of instance IDs
                      from a file; validate each against the dataset
  3. --n/--seed     — random reproducible sample (default mode)

Output is one instance ID per line, in dataset order, so downstream
filters/lookups are deterministic.

Usage:
    python scripts/sample_instances.py --subset verified --split test \
        --slice 0:5

    python scripts/sample_instances.py --subset verified --split test \
        --input-file my_list.txt

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


def resolve_dataset_path(subset: str, override: str | None) -> str:
    if override is not None:
        return override
    from minisweagent.run.benchmarks.swebench import DATASET_MAPPING
    return DATASET_MAPPING.get(subset, subset)


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--subset", default="verified",
                   choices=["verified", "lite", "full", "multimodal", "multilingual"])
    p.add_argument("--split", default="test")
    p.add_argument("--dataset", default=None,
                   help="override HF dataset name (default: princeton-nlp/SWE-bench_<subset>)")
    p.add_argument("--shuffle", action="store_true",
                   help="randomize the output order (default: keep dataset order)")

    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--slice", default=None,
                      help="contiguous slice of the dataset, Python slice syntax (e.g. 0:5, 50:60, 100:)")
    mode.add_argument("--input-file", default=None,
                      help="read newline-delimited instance IDs from this file")
    mode.add_argument("--n", type=int, default=None,
                      help="number of random instances to sample (default mode)")
    p.add_argument("--seed", type=int, default=1,
                      help="random seed for reproducible sampling (default: 1)")

    args = p.parse_args()

    dataset_path = resolve_dataset_path(args.subset, args.dataset)

    from datasets import load_dataset
    ds = load_dataset(dataset_path, split=args.split)
    all_ids = [d["instance_id"] for d in ds]
    n_total = len(all_ids)
    id_to_idx = {iid: i for i, iid in enumerate(all_ids)}

    sampled_ids: list[str]

    if args.slice is not None:
        try:
            start, stop = (int(x) if x else None for x in args.slice.split(":"))
            # Normalise so the user can write ":N" or "M:"
            if start is None: start = 0
            if stop is None:  stop = n_total
            if stop < 0:       stop = n_total + stop
        except ValueError:
            print(f"✗ bad --slice {args.slice!r}, expected M:N", file=sys.stderr)
            return 2
        if not (0 <= start < n_total) or not (0 <= stop <= n_total) or start > stop:
            print(f"✗ --slice {args.slice!r} out of range for dataset of size {n_total}",
                  file=sys.stderr)
            return 2
        sampled_ids = all_ids[start:stop]
        mode_label = f"slice={args.slice} ({len(sampled_ids)} instances)"

    elif args.input_file is not None:
        try:
            with open(args.input_file) as f:
                wanted = [line.strip() for line in f if line.strip()]
        except OSError as e:
            print(f"✗ cannot read --input-file {args.input_file}: {e}", file=sys.stderr)
            return 2
        unknown = [iid for iid in wanted if iid not in id_to_idx]
        if unknown:
            print(f"✗ {len(unknown)} IDs from {args.input_file} are not in the dataset "
                  f"(e.g. {unknown[:3]})", file=sys.stderr)
            return 2
        # preserve dataset order regardless of how the file was written
        sampled_ids = sorted(wanted, key=lambda iid: id_to_idx[iid])
        mode_label = f"input-file={args.input_file} ({len(sampled_ids)} instances)"

    else:
        n = args.n if args.n is not None else 5
        if n > n_total:
            print(f"✗ --n {n} > dataset size {n_total}", file=sys.stderr)
            return 1
        rng = random.Random(args.seed)
        picked = rng.sample(range(n_total), n)
        sampled_ids = [all_ids[i] for i in picked]
        mode_label = f"seed={args.seed} n={n}"

    if args.shuffle:
        rng = random.Random(args.seed)
        rng.shuffle(sampled_ids)

    print(f"# {mode_label}", file=sys.stderr)
    for iid in sampled_ids:
        print(iid)
    return 0


if __name__ == "__main__":
    sys.exit(main())
