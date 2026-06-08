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
# Source .env but do NOT overwrite variables already in the environment
# (so that cmdline `SWEBENCH_SEED=2 ./run.sh` wins over .env's default).
if [[ -z "${SWEBENCH_SEED:-}" || -z "${LLM_BASE_URL:-}" ]]; then
    set -a; source .env; set +a
    [[ -f .env.last_resolved ]] && set -a && source .env.last_resolved && set +a
fi
: "${SWEBENCH_SUBSET:=verified}"
: "${SWEBENCH_SPLIT:=test}"
: "${SWEBENCH_SEED:=1}"
: "${SWEBENCH_N:=5}"
: "${SWEBENCH_WORKERS:=1}"

ARCH="x86_64"
REMOTE_NAMESPACE="ghcr.io/epoch-research"
LOCAL_NAMESPACE="sweb.eval.x86_64"   # must match run_evaluation.sh
DATASET_NAME="princeton-nlp/SWE-bench_Verified"

mkdir -p "runs/${SWEBENCH_RUN_ID:-}"
SAMPLE_FILE="runs/${SWEBENCH_RUN_ID}/sampled_ids.txt"

# Resolve the sampled instance IDs.
python scripts/sample_instances.py \
    --subset "$SWEBENCH_SUBSET" --split "$SWEBENCH_SPLIT" \
    --n "$SWEBENCH_N" --seed "$SWEBENCH_SEED" \
    > "$SAMPLE_FILE" 2>/dev/null

COUNT=$(wc -l < "$SAMPLE_FILE" | tr -d ' ')
echo "→ pre-staging ${COUNT} image(s) from ${REMOTE_NAMESPACE} (seed=${SWEBENCH_SEED} n=${SWEBENCH_N})"
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

echo "→ verifying"
docker images --format '{{.Repository}}:{{.Tag}}' | grep "^${LOCAL_NAMESPACE}/" | sed 's/^/    /'
echo "→ sampled IDs saved to: $SAMPLE_FILE"
