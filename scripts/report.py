#!/usr/bin/env python3
"""Aggregate per-instance report.json files from the SWE-bench harness run
and produce a summary with a 95% confidence interval on the resolved
proportion, plus a projection of the expected score range on the full
SWE-bench Verified (500 instances).

Wilson score interval is used for the CI (better-behaved than the normal
approximation for small N and proportions near 0 or 1).

Usage:
    python scripts/report.py runs/<run_id>

Writes a human-readable summary to runs/<run_id>/report.txt.
"""
from __future__ import annotations

import json
import math
import sys
from pathlib import Path

VERIFIED_TOTAL = 500
Z_95 = 1.959963984540054   # two-sided 95% normal quantile


def wilson_ci(x: int, n: int, z: float = Z_95) -> tuple[float, float, float]:
    """Wilson score interval for binomial(p). Returns (p_hat, lo, hi)."""
    if n == 0:
        return (0.0, 0.0, 0.0)
    p_hat = x / n
    denom = 1 + z * z / n
    centre = (p_hat + z * z / (2 * n)) / denom
    half = z * math.sqrt(p_hat * (1 - p_hat) / n + z * z / (4 * n * n)) / denom
    return (p_hat, max(0.0, centre - half), min(1.0, centre + half))


def find_log_root(run_dir: Path) -> Path | None:
    """Locate the swebench-harness log root for this run.

    The harness writes per-instance artifacts under
    logs/run_evaluation/<model>/<id>/report.json. The model name is taken
    from preds.json (we read it from there), not from run_id, so the
    search is robust to changes in the harness layout.
    """
    candidates = [
        run_dir / "evaluation" / "logs" / "run_evaluation",
        run_dir / "evaluation" / "evaluation_results",
    ]
    return next((p for p in candidates if p.exists()), None)


def find_reports(log_root: Path, model_slug: str | None) -> list[Path]:
    """Return per-instance report.json paths under log_root.

    The harness layout is logs/run_evaluation/<model>/<id>/report.json. If
    model_slug is provided, we constrain the search to that subtree to
    avoid picking up reports from other runs sharing the same eval dir.
    """
    search_root = log_root / model_slug if model_slug else log_root
    if not search_root.exists():
        return sorted(log_root.rglob("report.json"))
    return sorted(search_root.rglob("report.json"))


def read_pred_trajectory_tokens(run_dir: Path, instance_id: str) -> tuple[int | None, int | None]:
    """Read prompt/completion token counts from a mini-swe-agent trajectory."""
    traj = run_dir / "inference" / instance_id / f"{instance_id}.traj.json"
    if not traj.exists():
        return (None, None)
    try:
        d = json.loads(traj.read_text())
    except Exception:
        return (None, None)
    # mini-swe-agent records the response under info._sa_summary or in the
    # last message's 'extra' field with a 'response' from litellm
    last = d.get("messages", [])[-1] if d.get("messages") else {}
    extra = last.get("extra", {}) if isinstance(last, dict) else {}
    usage = (extra.get("response") or {}).get("usage") or {}
    p = usage.get("prompt_tokens")
    c = usage.get("completion_tokens")
    return (p, c)


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: report.py runs/<run_id>", file=sys.stderr)
        return 1

    run_dir = Path(sys.argv[1])
    if not run_dir.is_dir():
        print(f"✗ {run_dir} is not a directory", file=sys.stderr)
        return 1

    log_root = find_log_root(run_dir)
    if log_root is None:
        print(f"✗ could not find log root under {run_dir}/evaluation", file=sys.stderr)
        return 1

    # Try to figure out the model slug used by the harness from preds.json,
    # so we only pick up this run's report.json files.
    model_slug: str | None = None
    preds_path = run_dir / "inference" / "preds.json"
    if preds_path.exists():
        try:
            d = json.loads(preds_path.read_text())
            first = next(iter(d.values()), {})
            name = first.get("model_name_or_path")
            if name:
                model_slug = str(name).replace("/", "__")
        except Exception:
            pass

    reports = find_reports(log_root, model_slug)
    if not reports:
        print(f"✗ no report.json files under {log_root}{'/'+model_slug if model_slug else ''}",
              file=sys.stderr)
        return 1

    resolved = 0
    total = 0
    per_instance: list[tuple[str, bool, str, int | None, int | None]] = []
    for rp in reports:
        d = json.loads(rp.read_text())
        for inst_id, r in d.items():
            total += 1
            ok = bool(r.get("resolved"))
            if ok:
                resolved += 1
            p_tok, c_tok = read_pred_trajectory_tokens(run_dir, inst_id)
            per_instance.append((inst_id, ok, "?", p_tok, c_tok))

    pct, lo, hi = wilson_ci(resolved, total)
    n_completed = total
    n_missing = max(0, VERIFIED_TOTAL - n_completed)
    expected_low = math.ceil(lo * VERIFIED_TOTAL)
    expected_high = math.floor(hi * VERIFIED_TOTAL)
    expected_point = round(pct * VERIFIED_TOTAL)

    # Token totals
    total_p = sum(p for _, _, _, p, _ in per_instance if p is not None)
    total_c = sum(c for _, _, _, _, c in per_instance if c is not None)
    have_tokens = any(p is not None for _, _, _, p, _ in per_instance)

    lines: list[str] = []
    lines.append(f"=== {run_dir.name} ===")
    lines.append(f"resolved: {resolved}/{total}  ({pct*100:.1f}%)")
    lines.append("")
    lines.append("95% confidence interval (Wilson score):")
    lines.append(f"  observed: [{lo*100:5.1f}%, {hi*100:5.1f}%]")
    lines.append(f"  expected score on full SWE-bench Verified ({VERIFIED_TOTAL} instances):")
    lines.append(f"    point estimate: {expected_point}/{VERIFIED_TOTAL}  ({pct*100:.1f}%)")
    lines.append(f"    95% CI range:   {expected_low}–{expected_high} / {VERIFIED_TOTAL}  "
                 f"({lo*100:.1f}%–{hi*100:.1f}%)")
    if n_missing:
        lines.append(f"  note: estimate assumes the {n_missing} unrun instances would score")
        lines.append(f"        at the same rate as the {n_completed} completed; widen the CI for the")
        lines.append(f"        full set to roughly  ±{1.96*math.sqrt(VERIFIED_TOTAL*0.25)/VERIFIED_TOTAL*100:.1f}%")
        lines.append(f"        (worst-case 50% prior).")
    if have_tokens:
        lines.append("")
        lines.append(f"tokens (from mini-swe-agent trajectories):")
        lines.append(f"  prompt:     {total_p:>9,}")
        lines.append(f"  completion: {total_c:>9,}")
        lines.append(f"  total:      {total_p + total_c:>9,}  "
                     f"(≈ {(total_p + total_c) / max(1,total):,.0f} tok/instance)")
    lines.append("")
    lines.append("per-instance:")
    for inst_id, ok, _, p_tok, c_tok in per_instance:
        mark = "✓" if ok else "✗"
        tok = ""
        if p_tok is not None and c_tok is not None:
            tok = f"  [{p_tok + c_tok:>6,} tok]"
        lines.append(f"  {mark} {inst_id}{tok}")

    report_text = "\n".join(lines) + "\n"
    print(report_text)

    out = run_dir / "report.txt"
    out.write_text(report_text)
    print(f"→ wrote {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
