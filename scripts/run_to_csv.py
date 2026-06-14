#!/usr/bin/env python3
"""Export per-instance metrics from a run dir to CSV.

Required columns:
  instance_id, pass, exit_status, inference_time_sec,
  llm_time_sec, llm_calls_count,
  llm_prompt_tokens_count, llm_cached_prompt_tokens_count,
  llm_completion_tokens_count

Usage:
    python run_to_csv.py <run_dir> [--out <path>] [--include-ungraded]

Where <run_dir> is e.g. runs/run100-20260609-100202.

'pass' comes from the swebench harness's per-instance report.json
(evaluation/logs/.../<id>/report.json). For instances that were never
graded (e.g. an empty-patch instance that the harness skipped), we fall
back to a best-effort: pass=False and inference/llm/token metrics come
from the traj alone. --include-ungraded is the default; pass
--graded-only to skip rows without an eval report.

'exit_status' is the mini-swe-agent termination reason, read from
traj.json["info"]["exit_status"]. Possible values (per
minisweagent/exceptions.py and agents/default.py):
  - ""            — empty (no traj / traj missing the field)
  - "Submitted"   — agent finished via the submit command
  - "LimitsExceeded" — step or cost limit hit
  - "TimeExceeded"  — wall-clock time limit hit (subclass of LimitsExceeded)
  - "FormatError"   — uncaught FormatError
  - "RepeatedFormatError" — too many format errors in a row
  - "UserInterruption"
If the traj is missing the field, falls back to any
inference/exit_statuses_*.yaml written by RunBatchProgressManager.
"""
from __future__ import annotations

import argparse
import csv
import json
from collections import defaultdict
from pathlib import Path
from typing import Any


def safe_get(d: Any, *keys, default=None):
    for k in keys:
        if not isinstance(d, dict):
            return default
        d = d.get(k)
    return d if d is not None else default


def find_eval_report(run_dir: Path, iid: str) -> Path | None:
    """Locate the per-instance report.json written by the harness."""
    pattern = (
        run_dir
        / "evaluation"
        / "logs"
        / "run_evaluation"
        / run_dir.name
        / "openai__deepseek-v4-flash"
        / iid
        / "report.json"
    )
    return pattern if pattern.exists() else None


def load_exit_status_yaml_map(inf_dir: Path) -> dict[str, str]:
    """Build iid -> exit_status from the first inference/exit_statuses_*.yaml.

    mini-swe-agent's RunBatchProgressManager writes one of these per run
    with shape {instances_by_exit_status: {<status>: [<iid>, ...]}}. We
    invert it for a per-instance lookup. Returns {} if no yaml is found
    or it can't be parsed.
    """
    import yaml

    yamls = sorted(inf_dir.glob("exit_statuses_*.yaml"))
    if not yamls:
        return {}
    try:
        data = yaml.safe_load(yamls[-1].read_text()) or {}
    except Exception:
        return {}
    inv: dict[str, str] = {}
    for status, iids in (data.get("instances_by_exit_status") or {}).items():
        for iid in iids or []:
            inv[iid] = status
    return inv


def summarize_traj(traj: dict) -> dict:
    """Extract per-traj metrics from messages."""
    msgs = traj.get("messages", [])

    # Build a (ts, role) timeline. mini-swe-agent records
    # `extra.timestamp` on tool messages; assistant messages may or may
    # not have it. Treat gaps between tool->assistant as LLM time, and
    # gaps between assistant->tool as tool time.
    prev_tool_ts = None
    last_asst_ts = None
    llm_time = 0.0
    first_ts = None
    last_ts = None

    prompt_tokens = 0
    completion_tokens = 0
    cached_tokens = 0
    n_calls = 0

    for m in msgs:
        ts = safe_get(m, "extra", "timestamp")
        if isinstance(ts, (int, float)):
            if first_ts is None:
                first_ts = ts
            last_ts = ts

        role = m.get("role")
        if role == "assistant":
            if prev_tool_ts is not None and isinstance(prev_tool_ts, (int, float)) and isinstance(ts, (int, float)):
                llm_time += ts - prev_tool_ts
            elif last_asst_ts is None and first_ts is not None and isinstance(ts, (int, float)):
                # first assistant turn: open gap from session start
                llm_time += ts - first_ts
            last_asst_ts = ts

            usage = safe_get(m, "extra", "response", "usage") or {}
            if usage:
                n_calls += 1
                prompt_tokens += usage.get("prompt_tokens", 0) or 0
                completion_tokens += usage.get("completion_tokens", 0) or 0
                cached_tokens += (
                    safe_get(usage, "prompt_tokens_details", "cached_tokens", default=0) or 0
                )

        elif role == "tool":
            if isinstance(ts, (int, float)):
                prev_tool_ts = ts

    wall = (last_ts - first_ts) if (first_ts is not None and last_ts is not None and last_ts >= first_ts) else 0
    return {
        "inference_time_sec": int(wall),
        "llm_time_sec": int(llm_time),
        "llm_calls_count": n_calls,
        "llm_prompt_tokens_count": prompt_tokens,
        "llm_cached_prompt_tokens_count": cached_tokens,
        "llm_completion_tokens_count": completion_tokens,
    }


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("run_dir")
    p.add_argument("--out", default=None,
                   help="Output CSV path (default: <run_dir>/results.csv)")
    p.add_argument("--graded-only", action="store_true",
                   help="Skip rows where no eval report.json exists")
    args = p.parse_args()

    run_dir = Path(args.run_dir).resolve()
    if not run_dir.is_dir():
        print(f"not a directory: {run_dir}")
        return 2

    inf_dir = run_dir / "inference"
    if not inf_dir.is_dir():
        print(f"no inference/ subdir in {run_dir}")
        return 2

    out_path = Path(args.out) if args.out else (run_dir / "results.csv")
    fields = [
        "instance_id",
        "pass",
        "exit_status",
        "inference_time_sec",
        "llm_time_sec",
        "llm_calls_count",
        "llm_prompt_tokens_count",
        "llm_cached_prompt_tokens_count",
        "llm_completion_tokens_count",
    ]

    # Fallback lookup for exit_status in case a traj is missing the field
    # (e.g. interrupted run, traj written by an older agent version).
    yaml_status = load_exit_status_yaml_map(inf_dir)

    # Collect per-instance rows, ordered by instance_id for determinism.
    rows: list[dict] = []
    for d in sorted(p for p in inf_dir.iterdir() if p.is_dir()):
        trajs = list(d.glob("*.traj.json"))
        if not trajs:
            continue
        traj = json.loads(trajs[0].read_text())
        iid = d.name

        eval_path = find_eval_report(run_dir, iid)
        if eval_path is not None:
            ev = json.loads(eval_path.read_text())[iid]
            passed = bool(ev.get("resolved", False))
        else:
            if args.graded_only:
                continue
            # Best-effort for ungraded (e.g. empty-patch instances that
            # the harness skipped). The model did submit, so it ran,
            # but no gold grading happened.
            passed = False

        status = safe_get(traj, "info", "exit_status", default="") or yaml_status.get(iid, "")

        m = summarize_traj(traj)
        rows.append({
            "instance_id": iid,
            "pass": passed,
            "exit_status": status,
            **m,
        })

    with out_path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for r in rows:
            w.writerow(r)

    # Also print a tiny summary to stdout.
    total = len(rows)
    n_pass = sum(1 for r in rows if r["pass"])
    by_status: dict[str, int] = defaultdict(int)
    for r in rows:
        by_status[r["exit_status"] or "(empty)"] += 1
    status_breakdown = ", ".join(
        f"{s}={n}" for s, n in sorted(by_status.items(), key=lambda kv: (-kv[1], kv[0]))
    )
    print(
        f"wrote {out_path}  rows={total}  pass={n_pass}  fail={total - n_pass}"
        f"  by_status: {status_breakdown}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
