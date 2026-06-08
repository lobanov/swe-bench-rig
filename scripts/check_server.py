#!/usr/bin/env python3
"""Probe the local OpenAI-compat LLM and auto-resolve LLM_MODEL.

Reads .env from the repo root if present. Writes LLM_MODEL to a
.env.last_resolved file so downstream shell scripts can source it.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def load_env() -> None:
    env_path = REPO_ROOT / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())


def probe(base: str, api_key: str) -> list[str]:
    req = urllib.request.Request(
        f"{base}/models",
        headers={"Authorization": f"Bearer {api_key}"},
    )
    with urllib.request.urlopen(req, timeout=10) as r:  # noqa: S310
        data = json.load(r)
    return [m["id"] for m in data.get("data", [])]


def main() -> int:
    load_env()
    base = os.environ.get("LLM_BASE_URL", "").rstrip("/")
    api_key = os.environ.get("LLM_API_KEY", "")
    if not base:
        print("✗ LLM_BASE_URL is not set; copy .env.example to .env", file=sys.stderr)
        return 1
    if not api_key:
        print("✗ LLM_API_KEY is not set; copy .env.example to .env", file=sys.stderr)
        return 1

    try:
        ids = probe(base, api_key)
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError) as e:
        print(f"✗ cannot reach {base}/models: {e}", file=sys.stderr)
        return 1

    if not ids:
        print(f"✗ {base} reports zero models", file=sys.stderr)
        return 1

    head = ids[:5]
    print(f"✓ {base} serves {len(ids)} model(s): {head}{'…' if len(ids) > 5 else ''}")

    existing = os.environ.get("LLM_MODEL", "").strip()
    if existing.startswith("openai/"):
        chosen = existing.split("/", 1)[1]
        print(f"  → using LLM_MODEL from env: openai/{chosen}")
    elif existing:
        chosen = existing
        print(f"  → using LLM_MODEL from env: openai/{chosen}")
    else:
        chosen = ids[0]
        (REPO_ROOT / ".env.last_resolved").write_text(f"LLM_MODEL=openai/{chosen}\n")
        print(f"  → auto-resolved LLM_MODEL=openai/{chosen} (first model listed)")
        print(f"  → wrote .env.last_resolved")
    return 0


if __name__ == "__main__":
    sys.exit(main())
