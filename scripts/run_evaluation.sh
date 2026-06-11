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
export PYTHONPATH="$(pwd)/config${PYTHONPATH:+:$PYTHONPATH}"
export MSWEA_SILENT_STARTUP=1

# Source .env line-by-line, only exporting vars not already in the
# environment. Lets cmdline overrides win while still filling in
# defaults for unset vars. LLM_MODEL is left for check_server.
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
: "${SWEBENCH_SPLIT:=test}"
: "${SWEBENCH_WORKERS:=1}"

# Must match LOCAL_NAMESPACE in pull_images.sh.
EVAL_NAMESPACE="sweb.eval.x86_64"

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
INFER_DIR="$REPO_ROOT/runs/${SWEBENCH_RUN_ID}/inference"
EVAL_DIR="$REPO_ROOT/runs/${SWEBENCH_RUN_ID}/evaluation"
RUN_DIR="$REPO_ROOT/runs/${SWEBENCH_RUN_ID}"
SAMPLE_FILE="$RUN_DIR/sampled_ids.txt"

if [[ ! -f "$INFER_DIR/preds.json" ]]; then
    echo "✗ $INFER_DIR/preds.json not found; run scripts/run_inference.sh first" >&2
    exit 1
fi

mkdir -p "$EVAL_DIR"

# Cross-check: warn if sampled_ids.txt differs from the IDs in preds.json.
if [[ -f "$SAMPLE_FILE" ]]; then
    SAMPLED=$(sort "$SAMPLE_FILE" | paste -sd, -)
    PREDICTED=$("$REPO_ROOT/.venv/bin/python" -c "import json; print(','.join(sorted(json.load(open('$INFER_DIR/preds.json')).keys())))")
    if [[ "$SAMPLED" != "$PREDICTED" ]]; then
        echo "⚠ sampled_ids.txt differs from predictions — eval will use predictions only" >&2
    fi
fi

cd "$EVAL_DIR"
"$REPO_ROOT/.venv/bin/python" -c "
import json
d = json.load(open('../inference/preds.json'))
for v in d.values():
    print(json.dumps(v))
" > preds.jsonl

# Restrict the harness to the same sampled IDs (paranoia: even if preds.jsonl
# contained extras, the harness would still process them).
SAMPLED_IDS=()
if [[ -f "$SAMPLE_FILE" ]]; then
    while read -r line; do SAMPLED_IDS+=("$line"); done < "$SAMPLE_FILE"
fi

# Skip instances that already have a report.json from a previous run.
SKIPPED=()
TO_RUN=()
for id in "${SAMPLED_IDS[@]}"; do
    rj="logs/run_evaluation/${SWEBENCH_RUN_ID}/openai__deepseek-v4-flash/${id}/report.json"
    if [[ -f "$rj" ]]; then
        SKIPPED+=("$id")
    else
        TO_RUN+=("$id")
    fi
done
echo "→ predictions:  $(wc -l < preds.jsonl) total"
echo "→ dataset:      princeton-nlp/SWE-bench_Verified (${SWEBENCH_SPLIT})"
echo "→ namespace:    ${EVAL_NAMESPACE}  (use locally retagged images)"
echo "→ cache_level:  env   (keep base+env, drop per-instance)"
echo "→ sample size:  ${#SAMPLED_IDS[@]};  already graded: ${#SKIPPED[@]};  to run: ${#TO_RUN[@]}"

if [[ ${#TO_RUN[@]} -eq 0 ]]; then
    echo "→ nothing to do"
    cd "$RUN_DIR" && .venv/bin/python scripts/report.py "$RUN_DIR"
    exit 0
fi

exec >"$RUN_DIR/eval.log" 2>&1
echo "→ predictions:  $(wc -l < preds.jsonl) total"
echo "→ dataset:      princeton-nlp/SWE-bench_Verified (${SWEBENCH_SPLIT})"
echo "→ namespace:    ${EVAL_NAMESPACE}  (use locally retagged images)"
echo "→ cache_level:  env   (keep base+env, drop per-instance)"
echo "→ already graded (skipping): ${SKIPPED[*]:-none}"
echo "→ to run: ${TO_RUN[*]}"

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
    --instance_ids "${TO_RUN[@]}"
