#!/usr/bin/env bash
# Run mini-swe-agent on the SWE-bench Verified sample selected by
# scripts/pull_images.sh. Reads the sampled instance IDs from
# runs/<run_id>/sampled_ids.txt and passes them to mini-extra as a
# regex filter, so the run is fully reproducible (same seed -> same IDs).
set -euo pipefail
cd "$(dirname "$0")/.."
source .venv/bin/activate
# Source .env but do NOT overwrite variables already in the environment.
if [[ -z "${LLM_MODEL:-}" || -z "${LLM_BASE_URL:-}" ]]; then
    set -a; source .env; set +a
    [[ -f .env.last_resolved ]] && set -a && source .env.last_resolved && set +a
    : "${SWEBENCH_RUN_ID:=smoke-$(date +%Y%m%d-%H%M%S)}"
    export SWEBENCH_RUN_ID
fi
: "${SWEBENCH_SUBSET:=verified}"
: "${SWEBENCH_SPLIT:=test}"
: "${SWEBENCH_WORKERS:=1}"
: "${AGENT_STEP_LIMIT:=250}"
: "${AGENT_COST_LIMIT:=3.0}"

if [[ -z "${LLM_MODEL:-}" ]]; then
    echo "✗ LLM_MODEL is empty; run scripts/check_server.py first" >&2
    exit 1
fi

export MSWEA_IMAGE_REGISTRY="ghcr.io/epoch-research/swe-bench.eval.x86_64"
export MSWEA_COST_TRACKING="ignore_errors"
export LITELLM_MODEL_REGISTRY_PATH="$(pwd)/config/litellm-registry.json"
export PYTHONPATH="$(pwd)/config${PYTHONPATH:+:$PYTHONPATH}"
export MSWEA_SILENT_STARTUP=1

# Render the YAML with ${VAR} placeholders substituted from the environment.
LOCAL_YAML_RENDERED="runs/${SWEBENCH_RUN_ID}/mini-swe-agent.local.rendered.yaml"
mkdir -p "$(dirname "$LOCAL_YAML_RENDERED")"
python -c "
import os, re, sys
src = open('config/mini-swe-agent.local.yaml').read()
def sub(m):
    name = m.group(1)
    val = os.environ.get(name, '')
    if not val:
        sys.stderr.write(f'⚠ env var \${name} is not set; leaving placeholder\n')
    return val
print(re.sub(r'\\\${([A-Z_][A-Z0-9_]*)}', sub, src))
" > "$LOCAL_YAML_RENDERED"

# Read the sampled IDs (written by pull_images.sh) and build a regex filter.
SAMPLE_FILE="runs/${SWEBENCH_RUN_ID}/sampled_ids.txt"
if [[ ! -f "$SAMPLE_FILE" ]]; then
    echo "✗ $SAMPLE_FILE not found; run scripts/pull_images.sh first" >&2
    exit 1
fi
IDS=$(paste -sd'|' "$SAMPLE_FILE")
FILTER_REGEX="^(${IDS})$"
N=$(wc -l < "$SAMPLE_FILE" | tr -d ' ')

RUN_DIR="runs/${SWEBENCH_RUN_ID}/inference"
mkdir -p "$RUN_DIR"
echo "→ rendered config: $LOCAL_YAML_RENDERED"
echo "→ filter regex    : $FILTER_REGEX"
echo "→ output          : $RUN_DIR"

mini-extra swebench \
    -c swebench.yaml \
    -c "$LOCAL_YAML_RENDERED" \
    --subset "$SWEBENCH_SUBSET" \
    --split  "$SWEBENCH_SPLIT" \
    --filter "$FILTER_REGEX" \
    --output "$RUN_DIR" \
    --workers "$SWEBENCH_WORKERS" \
    --model   "$LLM_MODEL"
