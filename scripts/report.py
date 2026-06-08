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


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: report.py runs/<run_id>", file=sys.stderr)
        return 1

    run_dir = Path(sys.argv[1])
    if not run_dir.is_dir():
        print(f"✗ {run_dir} is not a directory", file=sys.stderr)
        return 1

    candidates = [
        run_dir / "evaluation" / "logs" / "run_evaluation" / run_dir.name,
        run_dir / "evaluation" / "logs" / "run_evaluation",
        run_dir / "evaluation" / "evaluation_results",
    ]
    log_root = next((p for p in candidates if p.exists()), None)
    if log_root is None:
        print(f"✗ could not find log root under {run_dir}/evaluation", file=sys.stderr)
        for p in candidates:
            print(f"    - {p}", file=sys.stderr)
        return 1

    reports = sorted(log_root.rglob("report.json"))
    if not reports:
        print(f"✗ no report.json files under {log_root}", file=sys.stderr)
        return 1

    resolved = 0
    total = 0
    per_instance: list[tuple[str, bool, str]] = []
    for rp in reports:
        d = json.loads(rp.read_text())
        for inst_id, r in d.items():
            total += 1
            ok = bool(r.get("resolved"))
            if ok:
                resolved += 1
            per_instance.append((inst_id, ok, "?"))

    pct, lo, hi = wilson_ci(resolved, total)
    n_completed = total
    n_missing = max(0, VERIFIED_TOTAL - n_completed)
    expected_low = math.ceil(lo * VERIFIED_TOTAL)
    expected_high = math.floor(hi * VERIFIED_TOTAL)
    # expected point estimate (using point p_hat, not the centre of the CI)
    expected_point = round(pct * VERIFIED_TOTAL)

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
    lines.append("")
    lines.append("per-instance:")
    for inst_id, ok, _ in per_instance:
        mark = "✓" if ok else "✗"
        lines.append(f"  {mark} {inst_id}")

    report_text = "\n".join(lines) + "\n"
    print(report_text)

    out = run_dir / "report.txt"
    out.write_text(report_text)
    print(f"→ wrote {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
