#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

if ! command -v uv >/dev/null 2>&1; then
    echo "✗ uv not found. Install with: curl -LsSf https://astral.sh/uv/install.sh | sh" >&2
    exit 1
fi

if [[ ! -d .venv ]]; then
    echo "→ creating .venv (Python 3.11)"
    uv venv .venv --python 3.11
fi

# shellcheck disable=SC1091
source .venv/bin/activate

echo "→ installing mini-swe-agent from Git + support packages"
uv pip install -r requirements.txt

# swebench has Rust fixture files (Cargo.lock) that are not packaged correctly
# by `uv pip install` from git. Clone it locally and install editable so the
# fixture files are accessible at runtime.
if [[ ! -d vendor/swebench ]]; then
    echo "→ cloning swebench to vendor/ (one-time)"
    git clone --depth 1 https://github.com/SWE-bench/SWE-bench.git vendor/swebench
fi
echo "→ installing swebench editable from vendor/swebench"
uv pip install -e vendor/swebench

echo "→ verifying imports"
.venv/bin/python -c "
import minisweagent, swebench
from swebench.harness.constants import MAP_REPO_VERSION_TO_SPECS_PY
print('  minisweagent', minisweagent.__file__)
print('  swebench    ', swebench.__file__)
print('  swebench has', len(MAP_REPO_VERSION_TO_SPECS_PY), 'python repos')
"

echo
echo "✓ setup complete"
echo "  activate with:  source .venv/bin/activate"
echo "  run the rig:    ./run.sh"
