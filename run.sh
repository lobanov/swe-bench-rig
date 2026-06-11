#!/usr/bin/env bash
# End-to-end rig: setup (if needed) -> probe LLM -> pull images ->
# inference (mini-swe-agent) -> local grading (swebench harness) -> report.
set -euo pipefail
cd "$(dirname "$0")"

export PYTHONSTARTUP="$(pwd)/scripts/_mswea_image_patch.py"
# For scripts, PYTHONPATH-install config/sitecustomize.py so the image patch
# runs in every python invocation (including mini-extra).
export PYTHONPATH="$(pwd)/config${PYTHONPATH:+:$PYTHONPATH}"
# Also point litellm at our cost-tracking stub and silence the startup banner
# so the YAML rendering doesn't get contaminated.
export LITELLM_MODEL_REGISTRY_PATH="$(pwd)/config/litellm-registry.json"
export MSWEA_COST_TRACKING="ignore_errors"
export MSWEA_SILENT_STARTUP=1

# Make sure the venv takes precedence for mini-extra and the python CLI
export PATH="$(pwd)/.venv/bin:$PATH"

if [[ ! -d .venv ]]; then
    ./scripts/setup.sh
fi

if [[ ! -f .env ]]; then
    echo "✗ .env missing; copy .env.example to .env and edit" >&2
    exit 1
fi

# Source .env only if the run-shaping vars aren't already in the environment,
# so that cmdline overrides like `SWEBENCH_N=3 SWEBENCH_SEED=7 ./run.sh` win
# over .env's defaults. We source line-by-line and only export vars that are
# NOT already set in the environment, so a partial cmdline override
# (e.g. only SWEBENCH_N=3) is filled in with the rest of .env's defaults
# (LLM_BASE_URL, LLM_API_KEY, ...) without clobbering the override.
# Bash's `source .env` strips trailing comments from values; we replicate
# that here so values like `SWEBENCH_WORKERS=1   # 1 because ...` come out
# as the integer 1, not the whole tail.
while IFS='=' read -r key val; do
    [[ -z "$key" || "$key" =~ ^[[:space:]]*# ]] && continue
    # Strip trailing whitespace + inline `# comment` (bash assignment rule)
    val="${val%%#*}"
    val="${val%"${val##*[![:space:]]}"}"
    if [[ -z "${!key:-}" ]]; then
        export "$key=$val"
    fi
done < <(grep -E '^[A-Z_][A-Z0-9_]*=' .env)

# Default RUN_ID to a timestamp if not explicitly set in the environment
: "${SWEBENCH_RUN_ID:=smoke-$(date +%Y%m%d-%H%M%S)}"
export SWEBENCH_RUN_ID

echo
echo "═══ step 1/7  probe LLM ═══════════════════════════════════════"
.venv/bin/python scripts/check_server.py
[[ -f .env.last_resolved ]] && set -a && source .env.last_resolved && set +a

# Eager amd64-emulation check: epoch-research SWE-bench images are
# linux/amd64-only. On an arm64 host they only run if qemu-user-static
# is registered with binfmt_misc. Without it, `docker run` exits
# instantly, mini-swe-agent's container is gone before the first
# `docker exec`, and the agent burns its 250-step limit looping on
# "No such container" (see the failed django__django-11999 traj in
# runs/smoke/inference/). Skip with SKIP_AMD64_PROBE=1 once verified.
if [[ "${SKIP_AMD64_PROBE:-}" != "1" ]]; then
    if [[ -z "${MSWEA_AMD64_PROBE_IMAGE:-}" ]]; then
        # Prefer the first sampled instance image (also exercises the
        # ghcr.io auth/network path); fall back to alpine.
        SAMPLE_FILE="runs/${SWEBENCH_RUN_ID}/sampled_ids.txt"
        if [[ -f "$SAMPLE_FILE" ]] && [[ -s "$SAMPLE_FILE" ]]; then
            first_id=$(head -n1 "$SAMPLE_FILE")
            MSWEA_AMD64_PROBE_IMAGE="ghcr.io/epoch-research/swe-bench.eval.x86_64.${first_id}:latest"
        else
            MSWEA_AMD64_PROBE_IMAGE="alpine:latest"
        fi
    fi
    echo
    echo "═══ step 2/7  amd64 probe (qemu-user-static) ════════════════"
    echo "    docker run --rm --platform linux/amd64 ${MSWEA_AMD64_PROBE_IMAGE} uname -m"
    if ! probe_out=$(timeout 60 docker run --rm --platform linux/amd64 \
            "${MSWEA_AMD64_PROBE_IMAGE}" uname -m 2>&1); then
        echo >&2
        echo "✗ amd64 container failed to start." >&2
        echo "  epoch-research SWE-bench images are linux/amd64-only;" >&2
        echo "  on an arm64 host you need qemu-user-static registered with binfmt_misc." >&2
        echo >&2
        echo "  Fix on Ubuntu/Debian (requires sudo/root):" >&2
        echo "    sudo apt-get install -y qemu-user-static" >&2
        echo "    sudo docker run --privileged --rm tonistiegi/binfmt --install all" >&2
        echo >&2
        echo "  Or, on any docker host (no sudo if your user is in the" >&2
        echo "  'docker' group):" >&2
        echo "    docker run --privileged --rm tonistiigi/binfmt --install all" >&2
        echo >&2
        echo "  Probe output was:" >&2
        echo "${probe_out}" | sed 's/^/    /' >&2
        exit 1
    fi
    if [[ "${probe_out}" != "x86_64" ]]; then
        echo "✗ amd64 probe reported '${probe_out}' (expected 'x86_64')" >&2
        exit 1
    fi
    echo "    ✓ amd64 emulation works (qemu-x86_64 registered)"
fi

echo
echo "═══ step 3/7  pull + retag Docker images ═════════════════════"
./scripts/pull_images.sh

echo
echo "═══ step 4/7  inference (mini-swe-agent) ═════════════════════"
./scripts/run_inference.sh

echo
echo "═══ step 5/7  local grading (swebench harness) ════════════════"
./scripts/run_evaluation.sh

echo
echo "═══ step 6/7  report ══════════════════════════════════════════"
.venv/bin/python scripts/report.py "runs/${SWEBENCH_RUN_ID}"

echo
echo "═══ step 7/7  per-instance CSV ═══════════════════════════════"
.venv/bin/python scripts/run_to_csv.py "runs/${SWEBENCH_RUN_ID}"
