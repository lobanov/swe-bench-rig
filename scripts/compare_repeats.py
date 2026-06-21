#!/usr/bin/env python3
"""Compare instances that were run more than once — temp-0 divergence check.

The rig samples at temperature 0, which is *not* bit-deterministic on a real
server (batching, MoE routing, and non-associative float reductions on GPU/Metal
all perturb the logits). This finds instances with a completed trajectory in 2+
run dirs and quantifies how far the runs diverged — behaviourally (the sequence
of shell commands the agent issued) and in outcome (resolved / patch).

Because mini-swe-agent drives the model via tool-calls, the agent's *action* is
in `tool_calls[0].function.arguments.command`, NOT in `message.content` (which is
empty). The reasoning is in `reasoning_content`. We compare the command sequence;
the first index where commands differ is the behavioural fork point.

Deterministic — no model calls. Emits a markdown report + a .jsonl of records.

Usage:
    python compare_repeats.py runs/<a> runs/<b> [runs/<c> ...] [--out repeats_report.md]
"""
from __future__ import annotations

import argparse
import difflib
import glob
import json
import os
from collections import defaultdict
from pathlib import Path


def load_traj(run_dir: Path, iid: str) -> dict | None:
    p = run_dir / "inference" / iid / f"{iid}.traj.json"
    return json.loads(p.read_text()) if p.exists() else None


def command_of(m: dict) -> str:
    """Extract the shell command from a tool-call assistant message."""
    for tc in m.get("tool_calls") or []:
        args = tc.get("function", {}).get("arguments", "")
        try:
            return (json.loads(args) or {}).get("command", "") or ""
        except Exception:
            return args or ""
    # fall back to content (older/non-tool-call trajectories embed ```bash blocks)
    return ""


def commands(traj: dict) -> list[str]:
    return [command_of(m).strip() for m in traj.get("messages", []) if m.get("role") == "assistant" and (m.get("tool_calls") or command_of(m))]


def resolved_of(run_dir: Path, iid: str):
    hits = glob.glob(str(run_dir / "evaluation" / "logs" / "run_evaluation" / "*" / "*" / iid / "report.json"))
    if not hits:
        return None
    return json.load(open(hits[0])).get(iid, {}).get("resolved")


def patch_of(run_dir: Path, iid: str) -> str:
    pj = run_dir / "inference" / "preds.json"
    if not pj.exists():
        return ""
    return (json.loads(pj.read_text()).get(iid, {}) or {}).get("model_patch", "") or ""


def common_prefix_len(seqs: list[list[str]]) -> int:
    n = 0
    for tup in zip(*seqs):
        if len(set(tup)) == 1:
            n += 1
        else:
            break
    return n


def outcome_label(resolved, patch) -> str:
    if resolved:
        return "resolved"
    if not patch.strip():
        return "empty_patch"
    return "failed"


def compare_instance(iid: str, run_dirs: list[Path]) -> dict:
    present = [(rd, load_traj(rd, iid)) for rd in run_dirs]
    present = [(rd, t) for rd, t in present if t is not None]
    per_run = []
    cmd_seqs = []
    patches = []
    for rd, traj in present:
        cmds = commands(traj)
        patch = patch_of(rd, iid)
        resolved = resolved_of(rd, iid)
        cmd_seqs.append(cmds)
        patches.append(patch)
        per_run.append({
            "run_id": rd.name,
            "resolved": resolved,
            "outcome": outcome_label(resolved, patch),
            "exit_status": (traj.get("info", {}) or {}).get("exit_status"),
            "n_actions": len(cmds),
            "patch_len": len(patch),
            "patch_files": sorted({l.split()[2][2:] for l in patch.splitlines()
                                   if l.startswith("diff --git ") and len(l.split()) >= 3}),
        })

    # identical_command_prefix = how many leading shell commands were identical across
    # ALL runs. It is ALSO the 0-based index of the first command that differs, so 0 means
    # the runs forked at the very first action. commands_identical is True only when the
    # full command sequences matched end-to-end (no behavioural fork at all).
    prefix = common_prefix_len(cmd_seqs)
    commands_identical = not any(prefix < len(s) for s in cmd_seqs)
    outcomes = {r["outcome"] for r in per_run}
    resolveds = {r["resolved"] for r in per_run}
    # pairwise patch identity / similarity (first vs each other)
    patch_identical = len(set(patches)) == 1
    sim = None
    if len(patches) >= 2:
        sim = round(min(difflib.SequenceMatcher(None, patches[0], p).ratio() for p in patches[1:]), 3)

    div_cmds = None
    if not commands_identical:
        div_cmds = [(s[prefix] if prefix < len(s) else "<ended>") for s in cmd_seqs]

    return {
        "instance_id": iid,
        "n_runs": len(per_run),
        "runs": [r["run_id"] for r in per_run],
        "per_run": per_run,
        "identical_command_prefix": prefix,
        "commands_identical": commands_identical,
        "first_divergent_commands": div_cmds,
        "outcome_diverges": len(outcomes) > 1 or len(resolveds) > 1,
        "outcomes": sorted(outcomes),
        "patch_identical": patch_identical,
        "patch_similarity": sim,
    }


def render_report(records: list[dict], run_names: list[str]) -> str:
    n = len(records)
    fork0 = sum(1 for r in records if r["identical_command_prefix"] == 0 and not r["commands_identical"])
    odiv = sum(1 for r in records if r["outcome_diverges"])
    pid = sum(1 for r in records if r["patch_identical"])
    L = [
        "# Repeated-instance divergence (temperature 0)", "",
        f"Runs compared: {', '.join(run_names)}", "",
        f"- Instances run more than once: **{n}**",
        f"- Diverged at the very first action (#0): **{fork0}/{n}**",
        f"- Identical final patch across runs: **{pid}/{n}**",
        f"- **Outcome** diverged (resolved/empty/failed differs): **{odiv}/{n}**", "",
        "| Instance | Runs | Shared cmd prefix (= fork @, 0-idx) | Outcomes | Patch identical | Patch sim |",
        "|---|:--:|:--:|---|:--:|:--:|",
    ]
    for r in sorted(records, key=lambda x: (not x["outcome_diverges"], x["identical_command_prefix"])):
        L.append(
            f"| `{r['instance_id']}` | {r['n_runs']} | "
            f"{r['identical_command_prefix']}{'' if not r['commands_identical'] else ' (no fork)'} | "
            f"{' vs '.join(r['outcomes'])}"
            f"{' ⚠️' if r['outcome_diverges'] else ''} | {'yes' if r['patch_identical'] else 'no'} | "
            f"{r['patch_similarity'] if r['patch_similarity'] is not None else '—'} |"
        )
    L += ["", "## Per-instance detail", ""]
    for r in sorted(records, key=lambda x: x["identical_command_prefix"]):
        L.append(f"### `{r['instance_id']}`")
        for pr in r["per_run"]:
            L.append(f"- **{pr['run_id']}**: outcome=`{pr['outcome']}` resolved={pr['resolved']} "
                     f"actions={pr['n_actions']} patch_len={pr['patch_len']} files={pr['patch_files']}")
        if not r["commands_identical"]:
            L.append(f"- forked at action #{r['identical_command_prefix']} "
                     f"(0-indexed; shared the first {r['identical_command_prefix']} command(s)), then:")
            for run_id, cmd in zip(r["runs"], r["first_divergent_commands"] or []):
                L.append(f"    - `{run_id}`: `{cmd[:160]}`")
        else:
            L.append("- command sequences were byte-identical across runs (no behavioural fork)")
        L.append("")
    return "\n".join(L)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("run_dirs", nargs="+")
    ap.add_argument("--out", default="repeats_report.md", help="markdown report path (.jsonl written alongside)")
    args = ap.parse_args()

    run_dirs = [Path(d).resolve() for d in args.run_dirs]
    # iid -> run dirs that completed it
    done: dict[str, list[Path]] = defaultdict(list)
    for rd in run_dirs:
        for t in glob.glob(str(rd / "inference" / "*" / "*.traj.json")):
            done[os.path.basename(os.path.dirname(t))].append(rd)
    repeats = {iid: rds for iid, rds in done.items() if len(rds) > 1}
    if not repeats:
        print(f"No instances completed in >1 of: {[d.name for d in run_dirs]}")
        return 0

    records = [compare_instance(iid, rds) for iid, rds in sorted(repeats.items())]
    out = Path(args.out)
    out.write_text(render_report(records, [d.name for d in run_dirs]))
    jsonl = out.with_suffix(".jsonl")
    with jsonl.open("w") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")

    fork0 = sum(1 for r in records if r["identical_command_prefix"] == 0 and not r["commands_identical"])
    odiv = sum(1 for r in records if r["outcome_diverges"])
    print(f"repeated instances: {len(records)}  fork@0: {fork0}  outcome-diverged: {odiv}")
    print(f"wrote {out} and {jsonl}")
    for r in records:
        fork = "no-fork" if r["commands_identical"] else f"@{r['identical_command_prefix']}"
        print(f"  {r['instance_id']}: shared_prefix={r['identical_command_prefix']} "
              f"fork={fork} outcomes={'/'.join(r['outcomes'])}"
              f"{'  <-- OUTCOME DIVERGES' if r['outcome_diverges'] else ''}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
