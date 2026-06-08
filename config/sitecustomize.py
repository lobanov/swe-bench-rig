"""sitecustomize.py — auto-loaded by Python at startup from any dir on sys.path.

Redirects mini-swe-agent's docker image resolver to the ghcr.io/epoch-research
images when MSWEA_IMAGE_REGISTRY is set, so `mini-extra swebench` pulls from
the public epoch-research registry instead of trying to build locally.
"""
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
