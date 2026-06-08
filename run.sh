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

set -a; source .env; set +a

# Default RUN_ID to a timestamp if not explicitly set in the environment
: "${SWEBENCH_RUN_ID:=smoke-$(date +%Y%m%d-%H%M%S)}"
export SWEBENCH_RUN_ID

echo
echo "═══ step 1/5  probe LLM ═══════════════════════════════════════"
.venv/bin/python scripts/check_server.py
[[ -f .env.last_resolved ]] && set -a && source .env.last_resolved && set +a

echo
echo "═══ step 2/5  pull + retag Docker images ═════════════════════"
./scripts/pull_images.sh

echo
echo "═══ step 3/5  inference (mini-swe-agent) ═════════════════════"
./scripts/run_inference.sh

echo
echo "═══ step 4/5  local grading (swebench harness) ════════════════"
./scripts/run_evaluation.sh

echo
echo "═══ step 5/5  report ══════════════════════════════════════════"
.venv/bin/python scripts/report.py "runs/${SWEBENCH_RUN_ID}"
