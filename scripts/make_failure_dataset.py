#!/usr/bin/env python3
"""Build/refresh the SWE-bench failure-analysis HF dataset from run dirs.

Single idempotent driver — safe to re-run and to APPEND new runs. Everything is
keyed by `uid = "<run_id>/<instance_id>"` and every per-instance artifact lives
under a run-scoped path (`<kind>/<run_id>/<instance_id>.<ext>`), so adding more
runs never collides with existing data — even if a later run re-runs the same
instance_id.

What it does, deterministically, from the given run dirs:
  1. Stage per-instance artifacts (sanitized) into <out>/:
       trajectories/<run_id>/<iid>.traj.json   (full agent trajectory)
       transcripts/<run_id>/<iid>.md           (compact, rendered here)
       patches/<run_id>/<iid>.diff             (submitted patch; may be empty)
       test_outputs/<run_id>/<iid>.txt         (harness output; absent if empty patch)
  2. Build the per-instance table joining trajectory + harness report.json +
     patch, with a deterministic `failure_category`, the stamped
     `server_build_commit`/`server_build_url` provenance, and the verified
     `harness_false_negative` flag + `resolved_adjusted`.
  3. Merge qualitative diagnoses if present at diagnoses/<run_id>/<iid>.json
     (written by the trajectory-reading sub-agent pass); left null otherwise.
  4. Emit instances.jsonl, instances.csv, and RESULTS_TABLE.md (full summaries).

Appendable workflow (see the swebench-failure-analysis skill):
  # 0. (collaborators) pull the existing dataset so old rows/diagnoses are present:
  #      hf download <dataset> --type dataset --local-dir hf-dataset
  # 1. stage + build table; pass ONLY the run dirs you actually have. Rows for
  #    runs already in hf-dataset/instances.jsonl but not passed are preserved.
  python make_failure_dataset.py --out hf-dataset --server-commit <sha> \
      --server-url <url> --arch <arch> runs/<new-run> ...
  # 2. diagnose any uid whose diagnoses/<run_id>/<iid>.json is missing (sub-agents)
  # 3. re-run the SAME command to fold in the new diagnoses, then upload — as a PR
  #    for review: hf upload <dataset> hf-dataset . --type dataset --create-pr

Sanitization: the dummy local api_key and the operator's home path are redacted
from every staged text artifact automatically.
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from render_transcript import render as render_transcript  # noqa: E402

# Manually verified harness/environment false-negatives — failures caused by the
# test image, not the model's patch. Keyed by instance_id (stable across runs).
# See README "Harness false-negatives". Re-verify for any NEW astropy failures.
HARNESS_FALSE_NEGATIVES = {
    "astropy__astropy-8872": "pytest collection aborted: matplotlib LooseVersion error (0 tests collected); patch matches gold fix",
    "astropy__astropy-8707": "modern pytest rejects astropy's nose-style setup(self) (148 setup errors); env/version incompatibility",
    "astropy__astropy-7606": "model fixed the target test; 240/241 PASS_TO_PASS pass, the lone failure is an unrelated pre-existing numpy matmul test",
}

QUAL_FIELDS = ["root_cause_primary", "root_cause_secondary", "near_miss",
               "confidence", "what_model_did", "failure_summary", "evidence"]


def safe_get(d: Any, *keys, default=None):
    for k in keys:
        if not isinstance(d, dict):
            return default
        d = d.get(k)
    return d if d is not None else default


def sanitize(text: str) -> str:
    text = text.replace('"api_key": "lm-studio"', '"api_key": "REDACTED"')
    text = text.replace("lm-studio", "REDACTED")
    text = re.sub(r"/Users/dain\b", "/Users/USER", text)
    return text


def patch_stats(diff_text: str) -> dict:
    files: set[str] = set()
    added = removed = 0
    for line in diff_text.splitlines():
        if line.startswith("diff --git "):
            parts = line.split()
            if len(parts) >= 3:
                files.add(parts[2][2:] if parts[2].startswith("a/") else parts[2])
        elif line.startswith("+++ ") or line.startswith("--- "):
            continue
        elif line.startswith("+"):
            added += 1
        elif line.startswith("-"):
            removed += 1
    return {"patch_n_files": len(files), "patch_n_added_lines": added,
            "patch_n_removed_lines": removed, "patch_files": sorted(files)}


def summarize_traj(traj: dict) -> dict:
    msgs = traj.get("messages", [])
    prev_tool_ts = None
    llm_time = 0.0
    first_ts = last_ts = None
    prompt = completion = cached = n_calls = 0
    roles: dict[str, int] = {}
    for m in msgs:
        roles[m.get("role", "?")] = roles.get(m.get("role", "?"), 0) + 1
        ts = safe_get(m, "extra", "timestamp")
        if isinstance(ts, (int, float)):
            if first_ts is None:
                first_ts = ts
            last_ts = ts
        if m.get("role") == "assistant":
            if isinstance(prev_tool_ts, (int, float)) and isinstance(ts, (int, float)):
                llm_time += ts - prev_tool_ts
            elif first_ts is not None and isinstance(ts, (int, float)) and prev_tool_ts is None:
                llm_time += ts - first_ts
            usage = safe_get(m, "extra", "response", "usage") or {}
            if usage:
                n_calls += 1
                prompt += usage.get("prompt_tokens", 0) or 0
                completion += usage.get("completion_tokens", 0) or 0
                cached += safe_get(usage, "prompt_tokens_details", "cached_tokens", default=0) or 0
        elif m.get("role") == "tool":
            if isinstance(ts, (int, float)):
                prev_tool_ts = ts
    wall = (last_ts - first_ts) if (first_ts is not None and last_ts is not None and last_ts >= first_ts) else 0
    return {
        "inference_time_sec": int(wall), "llm_time_sec": int(llm_time),
        "llm_calls_count": n_calls, "llm_prompt_tokens_count": prompt,
        "llm_cached_prompt_tokens_count": cached, "llm_completion_tokens_count": completion,
        "cache_hit_rate": round(cached / prompt, 4) if prompt else 0.0,
        "n_messages": len(msgs), "n_assistant_turns": roles.get("assistant", 0),
        "n_tool_turns": roles.get("tool", 0),
    }


def classify(*, resolved, patch_len, patch_applied, p2p_fail, f2p_fail, exit_status) -> str:
    if resolved:
        return "resolved"
    if patch_len == 0:
        return "empty_patch"
    if patch_applied is False:
        return "patch_apply_failed"
    if p2p_fail and p2p_fail > 0:
        return "regression"
    if f2p_fail and f2p_fail > 0:
        return "unfixed"
    if exit_status in {"LimitsExceeded", "TimeExceeded", "Timeout", "RepeatedFormatError", "FormatError"}:
        return "limits"
    return "other"


def find_eval(run_dir: Path, iid: str, name: str) -> Path | None:
    hits = list(run_dir.glob(f"evaluation/logs/run_evaluation/*/*/{iid}/{name}"))
    return hits[0] if hits else None


def process_run(run_dir: Path, out: Path, server_commit: str, server_url: str, arch: str, population: str) -> list[dict]:
    inf = run_dir / "inference"
    run_id = run_dir.name
    preds = json.loads((inf / "preds.json").read_text()) if (inf / "preds.json").exists() else {}

    for kind in ("trajectories", "transcripts", "patches", "test_outputs"):
        (out / kind / run_id).mkdir(parents=True, exist_ok=True)

    rows = []
    for d in sorted(p for p in inf.iterdir() if p.is_dir()):
        iid = d.name
        trajs = list(d.glob("*.traj.json"))
        if not trajs:
            continue
        traj = json.loads(sanitize(trajs[0].read_text()))

        # stage artifacts (sanitized), run-scoped
        (out / "trajectories" / run_id / f"{iid}.traj.json").write_text(json.dumps(traj, indent=2))
        (out / "transcripts" / run_id / f"{iid}.md").write_text(sanitize(render_transcript(traj)))
        model_patch = safe_get(preds, iid, "model_patch", default="") or ""
        (out / "patches" / run_id / f"{iid}.diff").write_text(model_patch)
        to_src = find_eval(run_dir, iid, "test_output.txt")
        if to_src:
            (out / "test_outputs" / run_id / f"{iid}.txt").write_text(sanitize(to_src.read_text(errors="ignore")))

        pstats = patch_stats(model_patch) if model_patch.strip() else {
            "patch_n_files": 0, "patch_n_added_lines": 0, "patch_n_removed_lines": 0, "patch_files": []}

        report = find_eval(run_dir, iid, "report.json")
        resolved = patch_applied = None
        ts = {}
        if report is not None:
            rep = json.loads(report.read_text()).get(iid, {})
            resolved = bool(rep.get("resolved", False))
            patch_applied = rep.get("patch_successfully_applied")
            ts = rep.get("tests_status", {}) or {}

        def n(key, kind):
            v = safe_get(ts, key, kind)
            return len(v) if isinstance(v, list) else (v or 0)

        exit_status = safe_get(traj, "info", "exit_status", default="") or ""
        f2p_s, f2p_f = n("FAIL_TO_PASS", "success"), n("FAIL_TO_PASS", "failure")
        p2p_s, p2p_f = n("PASS_TO_PASS", "success"), n("PASS_TO_PASS", "failure")
        p2f = n("PASS_TO_FAIL", "failure") + n("PASS_TO_FAIL", "success")
        category = classify(resolved=resolved, patch_len=len(model_patch.strip()),
                            patch_applied=patch_applied, p2p_fail=p2p_f, f2p_fail=f2p_f,
                            exit_status=exit_status)

        rec = {
            "uid": f"{run_id}/{iid}", "instance_id": iid,
            "repo": iid.rsplit("-", 1)[0].replace("__", "/"), "run_id": run_id,
            "server_build_commit": server_commit, "server_build_url": server_url,
            "arch": arch, "population": population,
            "resolved": resolved if resolved is not None else False,
            "resolved_adjusted": bool((resolved or False) or iid in HARNESS_FALSE_NEGATIVES),
            "graded": report is not None,
            "harness_false_negative": iid in HARNESS_FALSE_NEGATIVES,
            "harness_false_negative_reason": HARNESS_FALSE_NEGATIVES.get(iid),
            "exit_status": exit_status, "failure_category": category,
            "patch_present": bool(model_patch.strip()),
            "patch_successfully_applied": patch_applied, **pstats,
            "fail_to_pass_success": f2p_s, "fail_to_pass_failure": f2p_f,
            "pass_to_pass_success": p2p_s, "pass_to_pass_failure": p2p_f,
            "pass_to_fail_count": p2f, **summarize_traj(traj),
            "instance_cost": safe_get(traj, "info", "model_stats", "instance_cost", default=0.0),
            "trajectory_path": f"trajectories/{run_id}/{iid}.traj.json",
            "transcript_path": f"transcripts/{run_id}/{iid}.md",
            "patch_path": f"patches/{run_id}/{iid}.diff",
            "test_output_path": f"test_outputs/{run_id}/{iid}.txt" if to_src else None,
        }
        # merge qualitative diagnosis if present
        dpath = out / "diagnoses" / run_id / f"{iid}.json"
        for f in QUAL_FIELDS:
            rec[f] = None
        if dpath.exists():
            diag = json.loads(dpath.read_text())
            for f in QUAL_FIELDS:
                rec[f] = diag.get(f)
        rows.append(rec)
    return rows


def write_table(rows: list[dict], path: Path) -> None:
    def emoji(r):
        if r["resolved"]:
            return "✅"
        if r["harness_false_negative"]:
            return "🟡"
        return "❌"

    srt = sorted(rows, key=lambda r: (r["repo"], not r["resolved_adjusted"], r["instance_id"]))
    lines = [
        "# Per-instance results", "",
        "✅ resolved  •  🟡 harness false-negative (env, not model)  •  ❌ genuine failure", "",
        "| Instance | Run | Resolved | Category | Root cause | Near-miss | What went wrong |",
        "|---|---|:--:|---|---|:--:|---|",
    ]
    for r in srt:
        summ = (r.get("failure_summary") or "").replace("|", "\\|").replace("\n", " ").strip()
        if r["resolved"]:
            summ = "—"
        nm = "•" if r.get("near_miss") else ""
        lines.append(
            f"| `{r['instance_id']}` | {r['run_id']} | {emoji(r)} | {r['failure_category']} | "
            f"{r.get('root_cause_primary') or ''} | {nm} | {summ} |"
        )
    path.write_text("\n".join(lines) + "\n")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("run_dirs", nargs="+")
    ap.add_argument("--out", default="hf-dataset")
    ap.add_argument("--server-commit", default="", help="LLM server build commit SHA")
    ap.add_argument("--server-url", default="", help="LLM server build commit URL")
    ap.add_argument("--arch", default="", help="Inference backend/arch (e.g. Metal, CUDA)")
    ap.add_argument("--population", default="unknown",
                    help="Instance population for the passed runs (e.g. prior-failures, sample)")
    ap.add_argument("--table", default="RESULTS_TABLE.md",
                    help="Path for the GitHub-pasteable table — a LOCAL convenience that duplicates "
                         "instances.jsonl, so it is written OUTSIDE --out and not part of the dataset. "
                         "Set empty to skip.")
    args = ap.parse_args()

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    built_run_ids = {Path(rd).resolve().name for rd in args.run_dirs}
    rows: list[dict] = []
    for rd in args.run_dirs:
        rows.extend(process_run(Path(rd).resolve(), out, args.server_commit, args.server_url, args.arch, args.population))

    # Preserve rows for runs NOT passed this invocation. This is what makes the
    # dataset appendable by someone who only `hf download`-ed it and has just
    # their own new run dir(s): the original runs' rows (and their already-staged
    # artifacts + non-regenerable diagnoses) are kept verbatim instead of dropped.
    existing = out / "instances.jsonl"
    if existing.exists():
        preserved = 0
        for line in existing.read_text().splitlines():
            if not line.strip():
                continue
            r = json.loads(line)
            if r.get("run_id") not in built_run_ids:
                rows.append(r)
                preserved += 1
        if preserved:
            kept_runs = sorted({r["run_id"] for r in rows if r["run_id"] not in built_run_ids})
            print(f"preserved {preserved} rows from runs not passed this invocation: {', '.join(kept_runs)}")
    rows.sort(key=lambda r: r["uid"])

    with (out / "instances.jsonl").open("w") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")

    csv_fields = [
        "uid", "instance_id", "repo", "run_id", "population", "arch", "server_build_commit", "resolved",
        "resolved_adjusted", "harness_false_negative", "failure_category",
        "root_cause_primary", "root_cause_secondary", "near_miss", "confidence",
        "exit_status", "patch_successfully_applied", "patch_n_files", "patch_n_added_lines",
        "patch_n_removed_lines", "fail_to_pass_success", "fail_to_pass_failure",
        "pass_to_pass_success", "pass_to_pass_failure", "inference_time_sec", "llm_time_sec",
        "llm_calls_count", "llm_prompt_tokens_count", "llm_cached_prompt_tokens_count",
        "llm_completion_tokens_count", "cache_hit_rate", "n_messages", "failure_summary",
    ]
    with (out / "instances.csv").open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=csv_fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)

    if args.table and Path(args.table).resolve() != (out / "RESULTS_TABLE.md").resolve():
        write_table(rows, Path(args.table))
    elif args.table:
        print("refusing to write the results table inside --out (it duplicates instances.jsonl); "
              "pass --table <path outside the dataset> or --table '' to skip")

    from collections import Counter
    n = len(rows)
    res = sum(1 for r in rows if r["resolved"])
    adj = sum(1 for r in rows if r["resolved_adjusted"])
    diagnosed = sum(1 for r in rows if r.get("root_cause_primary"))
    cats = Counter(r["failure_category"] for r in rows)
    print(f"instances={n}  resolved={res} ({res/n*100:.1f}%)  adjusted={adj} ({adj/n*100:.1f}%)  diagnosed={diagnosed}/{n}")
    print("category:", dict(cats.most_common()))
    missing = [r["uid"] for r in rows if not r.get("root_cause_primary")]
    if missing:
        print(f"NEED DIAGNOSIS ({len(missing)}): write diagnoses/<run_id>/<iid>.json for:")
        for u in missing:
            print("  -", u)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
