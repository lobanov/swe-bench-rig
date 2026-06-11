#!/usr/bin/env python3
"""Render config/mini-swe-agent.local.yaml with ${VAR} placeholders substituted
from the current environment. Writes the rendered YAML to stdout.

This is used by scripts/run_inference.sh; extracted so it can be tested and
reused (e.g. for run_evaluation.sh if we ever pass yaml configs there too).
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SOURCE = REPO_ROOT / "config" / "mini-swe-agent.local.yaml"
PLACEHOLDER_RE = re.compile(r"\$\{([A-Z_][A-Z0-9_]*)\}")


def render(text: str) -> str:
    missing: list[str] = []

    def sub(m: re.Match) -> str:
        name = m.group(1)
        val = os.environ.get(name, "")
        if not val:
            missing.append(name)
        return val

    out = PLACEHOLDER_RE.sub(sub, text)
    if missing:
        print(
            f"⚠ env vars not set, left as placeholders: {sorted(set(missing))}",
            file=sys.stderr,
        )
    return out


def main() -> int:
    if not SOURCE.exists():
        print(f"✗ {SOURCE} not found", file=sys.stderr)
        return 1
    sys.stdout.write(render(SOURCE.read_text()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
