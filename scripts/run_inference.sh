#!/usr/bin/env bash
# Run mini-swe-agent on the SWE-bench Verified sample selected by
# scripts/pull_images.sh. Reads the sampled instance IDs from
# runs/<run_id>/sampled_ids.txt and passes them to mini-extra as a
# regex filter, so the run is fully reproducible (same seed -> same IDs).
set -euo pipefail
cd "$(dirname "$0")/.."
source .venv/bin/activate
export PYTHONPATH="$(pwd)/config${PYTHONPATH:+:$PYTHONPATH}"
export MSWEA_SILENT_STARTUP=1

# This script does `exec >inference.log 2>&1` below to capture mini-extra
# output, which also swallows any fatal error from the terminal. Define
# an ERR handler that writes diagnostics to fd 3 (the original stderr,
# captured just before the exec) so a failure here is visible and
# points at the captured log.
on_err() {
    local exit_code=$?
    local line_no=${BASH_LINENO[0]}
    echo "✗ $0: command failed (exit $exit_code) at line $line_no" >&3
    echo "    last command: ${BASH_COMMAND}" >&3
    if [[ -n "${LOG_FILE:-}" ]] && [[ -f "$LOG_FILE" ]]; then
        echo "--- tail of $LOG_FILE ---" >&3
        tail -n 30 "$LOG_FILE" >&3
        echo "--- end tail ---" >&3
    fi
}

# Source .env line-by-line, only exporting vars not already in the
# environment. Lets cmdline overrides win while still filling in
# defaults for unset vars. LLM_MODEL is intentionally left for
# check_server to resolve from /v1/models.
while IFS='=' read -r key val; do
    [[ -z "$key" || "$key" =~ ^[[:space:]]*# ]] && continue
    [[ "$key" == "LLM_MODEL" ]] && continue
    # Strip trailing inline `# comment` (bash assignment rule)
    val="${val%%#*}"
    val="${val%"${val##*[![:space:]]}"}"
    if [[ -z "${!key:-}" ]]; then
        export "$key=$val"
    fi
done < <(grep -E '^[A-Z_][A-Z0-9_]*=' .env)
[[ -f .env.last_resolved ]] && source .env.last_resolved
: "${SWEBENCH_RUN_ID:=smoke-$(date +%Y%m%d-%H%M%S)}"
export SWEBENCH_RUN_ID
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

# Render the local YAML (substitute ${VAR} from env).
LOCAL_YAML_RENDERED="runs/${SWEBENCH_RUN_ID}/mini-swe-agent.local.rendered.yaml"
mkdir -p "$(dirname "$LOCAL_YAML_RENDERED")"
.venv/bin/python scripts/_render_yaml.py > "$LOCAL_YAML_RENDERED"

# Read the sampled IDs (written by pull_images.sh) and build a regex filter.
SAMPLE_FILE="runs/${SWEBENCH_RUN_ID}/sampled_ids.txt"
if [[ ! -f "$SAMPLE_FILE" ]]; then
    echo "✗ $SAMPLE_FILE not found; run scripts/pull_images.sh first" >&2
    exit 1
fi
N=$(wc -l < "$SAMPLE_FILE" | tr -d ' ')
IDS=$(paste -sd'|' "$SAMPLE_FILE")
FILTER_REGEX="^(${IDS})$"

RUN_DIR="runs/${SWEBENCH_RUN_ID}/inference"
mkdir -p "$RUN_DIR"
echo "→ rendered config: $LOCAL_YAML_RENDERED"
echo "→ filter regex    : $FILTER_REGEX"
echo "→ output          : $RUN_DIR"

# mini-extra logs go to runs/<run_id>/inference.log so they survive the
# inference exiting (mini-swe-agent only flushes its file handler at exit).
LOG_FILE="$RUN_DIR/../inference.log"
exec 3>&2  # preserve original stderr for the ERR trap
exec >"$LOG_FILE" 2>&1
trap 'on_err' ERR

mini-extra swebench \
    -c swebench.yaml \
    -c "$LOCAL_YAML_RENDERED" \
    --subset "$SWEBENCH_SUBSET" \
    --split  "$SWEBENCH_SPLIT" \
    --filter "$FILTER_REGEX" \
    --output "$RUN_DIR" \
    --workers "$SWEBENCH_WORKERS" \
    --model   "$LLM_MODEL"
