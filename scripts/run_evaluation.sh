#!/usr/bin/env bash
# Run the SWE-bench harness grading step locally.
# - Reads preds.json produced by run_inference.sh
# - Starts one Docker container per instance
# - Applies the model patch + the gold test patch
# - Runs the eval script and grades the result
# - Writes per-instance report.json and a top-level summary
#
# Uses the locally-retagged images produced by pull_images.sh.
# Note: we cannot use --namespace none (the harness treats None as
# "build env images locally" and tries to build them, which fails on
# arm64). We use a non-None namespace that matches the local image
# repository name; the harness then looks up
#   <namespace>/sweb.eval.<arch>.<id_with_1776>:latest
# which is exactly what pull_images.sh produced.
set -euo pipefail
cd "$(dirname "$0")/.."
source .venv/bin/activate
# Source .env but do NOT overwrite variables already in the environment
# (so that cmdline `SWEBENCH_RUN_ID=foo ./run.sh` wins over .env's default).
if [[ -z "${SWEBENCH_RUN_ID:-}" || "${SWEBENCH_RUN_ID}" == "smoke" ]]; then
    set -a; source .env; set +a
    [[ -f .env.last_resolved ]] && set -a && source .env.last_resolved && set +a
    : "${SWEBENCH_RUN_ID:=smoke-$(date +%Y%m%d-%H%M%S)}"
    export SWEBENCH_RUN_ID
fi
# Defaults for vars not in .env (so `set -u` is happy)
: "${SWEBENCH_SPLIT:=test}"
: "${SWEBENCH_WORKERS:=2}"

# Must match LOCAL_NAMESPACE in pull_images.sh.
EVAL_NAMESPACE="sweb.eval.x86_64"

INFER_DIR="runs/${SWEBENCH_RUN_ID}/inference"
EVAL_DIR="runs/${SWEBENCH_RUN_ID}/evaluation"

if [[ ! -f "$INFER_DIR/preds.json" ]]; then
    echo "✗ $INFER_DIR/preds.json not found; run scripts/run_inference.sh first" >&2
    exit 1
fi

mkdir -p "$EVAL_DIR"
cd "$EVAL_DIR"

python -c "
import json
d = json.load(open('../inference/preds.json'))
for v in d.values():
    print(json.dumps(v))
" > preds.jsonl

# Restrict the harness to the same sampled IDs (paranoia: even if preds.jsonl
# contained extras, the harness would still process them, but this keeps
# logs/sandbox work to the minimum).
SAMPLE_FILE="runs/${SWEBENCH_RUN_ID}/sampled_ids.txt"
SAMPLED_IDS=()
if [[ -f "$SAMPLE_FILE" ]]; then
    while read -r line; do SAMPLED_IDS+=("$line"); done < "$SAMPLE_FILE"
fi

echo "→ predictions: $(wc -l < preds.jsonl) lines"
echo "→ dataset:     princeton-nlp/SWE-bench_Verified (${SWEBENCH_SPLIT})"
echo "→ namespace:   ${EVAL_NAMESPACE}  (use locally retagged images)"
echo "→ cache_level: env   (keep base+env, drop per-instance)"

# Build the --instance_ids arg only if we have a sample file
if [[ ${#SAMPLED_IDS[@]} -gt 0 ]]; then
    INSTANCE_ID_ARGS=(--instance_ids "${SAMPLED_IDS[@]}")
else
    INSTANCE_ID_ARGS=()
fi

python -m swebench.harness.run_evaluation \
    --dataset_name     princeton-nlp/SWE-bench_Verified \
    --split            "$SWEBENCH_SPLIT" \
    --predictions_path preds.jsonl \
    --run_id           "$SWEBENCH_RUN_ID" \
    --namespace        "${EVAL_NAMESPACE}" \
    --cache_level      env \
    --max_workers      "$SWEBENCH_WORKERS" \
    --timeout          1800 \
    --report_dir       . \
    "${INSTANCE_ID_ARGS[@]}"
