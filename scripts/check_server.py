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

    # Always (re)write the litellm cost-registry file with the resolved
    # model id so litellm does not emit its "Provider List" warning at
    # every litellm.completion() call. Cost is zero for a local LLM.
    max_ctx = int(os.environ.get("LLM_MAX_CONTEXT", "131072") or "131072")
    write_litellm_registry(REPO_ROOT / "config" / "litellm-registry.json", chosen, max_ctx)
    print(f"  → wrote config/litellm-registry.json with model={chosen}")
    return 0


def write_litellm_registry(path: Path, model_id: str, max_tokens: int) -> None:
    """Write a litellm-compatible model-registry JSON for the local model.

    litellm warns "Provider List: https://docs.litellm.ai/docs/providers"
    whenever a model is not in its own internal registry. Registering a
    matching entry silences the warning and lets cost lookups succeed
    (which keeps the rig compatible with mini-swe-agent's default
    cost-tracking path; we additionally set MSWEA_COST_TRACKING=ignore_errors
    to bypass cost lookups entirely).
    """
    import json
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        model_id: {
            "max_tokens": max_tokens,
            "max_input_tokens": max_tokens,
            "input_cost_per_token": 0.0,
            "output_cost_per_token": 0.0,
            "litellm_provider": "openai",
            "mode": "chat",
        }
    }
    path.write_text(json.dumps(payload, indent=2) + "\n")


if __name__ == "__main__":
    sys.exit(main())
