#!/usr/bin/env python3
"""Render a compact, human-readable markdown transcript from a .traj.json.

Strips the repeated cached system/instance prompt and shows the conversation
as a sequence of steps: each assistant turn (its reasoning + the shell command
it issued) followed by the truncated tool output. Much smaller than the raw
trajectory and far easier for a human (or a reviewing agent) to follow.

Usage:
    python render_transcript.py <traj.json> [--out <md>] [--obs-limit N]
"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path


def truncate(text: str, limit: int) -> str:
    text = text or ""
    if len(text) <= limit:
        return text
    head = text[: limit // 2]
    tail = text[-limit // 2 :]
    return f"{head}\n... [truncated {len(text) - limit} chars] ...\n{tail}"


def render(traj: dict, obs_limit: int = 1800) -> str:
    info = traj.get("info", {})
    iid = traj.get("instance_id", "?")
    msgs = traj.get("messages", [])

    out: list[str] = []
    out.append(f"# Trajectory: {iid}")
    out.append("")
    out.append(f"- exit_status: `{info.get('exit_status')}`")
    out.append(f"- api_calls: {json.dumps(info.get('model_stats', {}))}")
    sub = info.get("submission")
    out.append(f"- submission: {'(empty)' if not (sub or '').strip() else str(len(sub)) + ' chars'}")
    out.append("")

    step = 0
    for m in msgs:
        role = m.get("role")
        content = m.get("content", "")
        if isinstance(content, list):  # some providers chunk content
            content = "\n".join(c.get("text", "") if isinstance(c, dict) else str(c) for c in content)
        if role in {"system", "user"} and step == 0:
            # the big setup prompt — show only a short head once
            if role == "user":
                out.append("## Task (problem statement, truncated)")
                out.append("```")
                out.append(truncate(content, 1200))
                out.append("```")
                out.append("")
            continue
        if role == "assistant":
            step += 1
            out.append(f"## Step {step} — assistant")
            out.append(truncate(content.strip(), 2400))
            out.append("")
        elif role == "tool":
            out.append("**observation:**")
            out.append("```")
            out.append(truncate(content.strip(), obs_limit))
            out.append("```")
            out.append("")
        elif role == "exit":
            out.append(f"## Exit\n{truncate(content.strip(), 800)}")
            out.append("")
    return "\n".join(out)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("traj")
    ap.add_argument("--out", default=None)
    ap.add_argument("--obs-limit", type=int, default=1800)
    args = ap.parse_args()
    traj = json.loads(Path(args.traj).read_text())
    md = render(traj, obs_limit=args.obs_limit)
    if args.out:
        Path(args.out).write_text(md)
        print(f"wrote {args.out} ({len(md)} chars)")
    else:
        print(md)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
