"""Monkey-patch mini-swe-agent's image-name resolver.

Loaded via PYTHONSTARTUP before mini-extra swebench runs. If
MSWEA_IMAGE_REGISTRY is set (e.g. "ghcr.io/epoch-research/sweb-bench.eval.x86_64"),
it rewrites the default image-name function so each instance uses
"<prefix>.<instance_id>:latest" instead of docker.io/swebench/... .

No upstream files are modified.
"""
from __future__ import annotations

import os

if os.environ.get("MSWEA_IMAGE_REGISTRY"):
    try:
        from minisweagent.run.benchmarks import swebench as _m
    except Exception:
        pass
    else:
        _original = _m.get_swebench_docker_image_name
        _prefix = os.environ["MSWEA_IMAGE_REGISTRY"]

        def _patched(instance):
            if instance.get("image_name") or instance.get("docker_image"):
                return _original(instance)
            return f"{_prefix}.{instance['instance_id']}:latest"

        _m.get_swebench_docker_image_name = _patched
