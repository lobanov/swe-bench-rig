#!/usr/bin/env bash
# Pull SWE-bench Verified instance images from ghcr.io/epoch-research and
# retag them to the names the SWE-bench harness expects.
#
# The harness builds TestSpec.instance_image_key as:
#   key = f"sweb.eval.{arch}.{instance_id}:tag"
#   if is_remote_image (namespace is not None):
#       key = f"{namespace}/{key}".replace("__", "_1776_")
#
# With namespace='sweb.eval.x86_64' (any non-None value, to skip the
# build-images branch in run_evaluation.main), the harness looks for:
#   sweb.eval.x86_64/sweb.eval.x86_64.<id_with_1776>:latest
# which is what we tag below.
#
# The list of instance IDs is resolved by sample_instances.py using a
# fixed seed, so reruns with the same SWEBENCH_SEED/SWEBENCH_N pick the
# same instances. Sampled IDs are written to runs/<run_id>/sampled_ids.txt
# so downstream steps (inference, evaluation) can use the same sample.
set -euo pipefail
cd "$(dirname "$0")/.."
source .venv/bin/activate
export PYTHONPATH="$(pwd)/config${PYTHONPATH:+:$PYTHONPATH}"
export MSWEA_SILENT_STARTUP=1

# Source .env line-by-line, only exporting vars not already in the
# environment. This lets cmdline overrides like
# `SWEBENCH_SEED=2 SWEBENCH_N=100 ./run.sh` win over .env's defaults
# for the cmdline-set vars while still pulling in defaults (e.g.
# LLM_BASE_URL) for vars the caller didn't set.
while IFS='=' read -r key val; do
    [[ -z "$key" || "$key" =~ ^[[:space:]]*# ]] && continue
    [[ "$key" == "LLM_MODEL" ]] && continue   # LLM_MODEL is set by check_server from /v1/models
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
: "${SWEBENCH_SEED:=1}"
: "${SWEBENCH_N:=5}"
: "${SWEBENCH_SLICE:=}"
: "${SWEBENCH_INPUT_FILE:=}"
: "${SWEBENCH_WORKERS:=1}"

ARCH="x86_64"
REMOTE_NAMESPACE="ghcr.io/epoch-research"
LOCAL_NAMESPACE="sweb.eval.x86_64"   # must match run_evaluation.sh
DATASET_NAME="princeton-nlp/SWE-bench_Verified"

RUN_DIR="runs/${SWEBENCH_RUN_ID}"
mkdir -p "$RUN_DIR"
SAMPLE_FILE="$RUN_DIR/sampled_ids.txt"

# Resolve the sampled instance IDs. Three modes (mutually exclusive):
#   SWEBENCH_SLICE=M:N     — deterministic contiguous slice of the dataset
#   SWEBENCH_INPUT_FILE=…  — explicit newline-delimited list
#   SWEBENCH_N=N (default) — random sample of N (with SWEBENCH_SEED)
SAMPLE_ARGS=(
    --subset "$SWEBENCH_SUBSET"
    --split  "$SWEBENCH_SPLIT"
)
if [[ -n "$SWEBENCH_SLICE" ]]; then
    SAMPLE_ARGS+=(--slice "$SWEBENCH_SLICE")
    MODE_LABEL="slice=${SWEBENCH_SLICE}"
elif [[ -n "$SWEBENCH_INPUT_FILE" ]]; then
    SAMPLE_ARGS+=(--input-file "$SWEBENCH_INPUT_FILE")
    MODE_LABEL="input-file=${SWEBENCH_INPUT_FILE}"
else
    SAMPLE_ARGS+=(--n "$SWEBENCH_N" --seed "$SWEBENCH_SEED")
    MODE_LABEL="seed=${SWEBENCH_SEED} n=${SWEBENCH_N}"
fi

# Stderr carries the mode label; stdout is the IDs. Redirect both.
.venv/bin/python scripts/sample_instances.py "${SAMPLE_ARGS[@]}" \
    > "$SAMPLE_FILE" 2>"$RUN_DIR/sample.log"

COUNT=$(wc -l < "$SAMPLE_FILE" | tr -d ' ')
echo "→ pre-staging ${COUNT} image(s) from ${REMOTE_NAMESPACE} (${MODE_LABEL})"
sed 's/^/    - /' "$SAMPLE_FILE"

# Pull + retag, skipping ones that are already in place. Tee all output
# to runs/<run_id>/pull.log for post-mortem.
exec >"$RUN_DIR/pull.log" 2>&1
echo "→ pre-staging ${COUNT} image(s) from ${REMOTE_NAMESPACE} (${MODE_LABEL})"
sed 's/^/    - /' "$SAMPLE_FILE"

while read -r id; do
    [[ -z "$id" ]] && continue
    src="${REMOTE_NAMESPACE}/swe-bench.eval.${ARCH}.${id,,}:latest"
    compat_id="${id//__/_1776_}"
    dst="${LOCAL_NAMESPACE}/sweb.eval.${ARCH}.${compat_id}:latest"
    if docker image inspect "$dst" >/dev/null 2>&1; then
        echo "  ✓ $dst already present"
        continue
    fi
    docker pull "$src"
    docker tag "$src" "$dst"
    echo "  ✓ $dst"
done < "$SAMPLE_FILE"

echo "→ sampled IDs saved to: $SAMPLE_FILE"
echo "→ final image count: $(docker images --format '{{.Repository}}:{{.Tag}}' | grep -c "^${LOCAL_NAMESPACE}/")"
