"""sitecustomize.py — auto-loaded by Python at startup from any dir on sys.path.

Redirects mini-swe-agent's docker image resolver to the ghcr.io/epoch-research
images when MSWEA_IMAGE_REGISTRY is set, so `mini-extra swebench` pulls from
the public epoch-research registry instead of trying to build locally.
Also silences litellm's "Provider List" debug banner and the
"This model isn't mapped yet" cost-lookup warnings, which otherwise spam
the run logs.
"""
import os

# Honor MSWEA_SILENT_STARTUP before importing minisweagent so the startup
# banner does not contaminate scripts that capture stdout (e.g. the
# sampled_ids.txt file in pull_images.sh).
os.environ.setdefault("MSWEA_SILENT_STARTUP", "1")

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


# Suppress litellm's debug banner ("Provider List: ...", "This model isn't
# mapped yet", etc.) so the rig's logs aren't polluted. The rig also runs
# with MSWEA_COST_TRACKING=ignore_errors so cost lookups are skipped, but
# the warning fires *before* that check in some code paths.
try:
    import litellm
    litellm.suppress_debug_info = True
except Exception:
    pass
