# SWE-Bench Verified Rig for a Local OpenAI-Compatible LLM

A reusable, scripted rig that uses the upstream `mini-swe-agent` and `SWE-bench`
tools to verify a local OpenAI-compatible LLM inference server on the
[SWE-bench Verified](https://huggingface.co/datasets/princeton-nlp/SWE-bench_Verified)
benchmark, against the optimized Docker images from
[epoch.ai/latest/swebench-docker](https://epoch.ai/latest/swebench-docker)
(public registry at `ghcr.io/epoch-research/sweb-bench.eval.x86_64.<id>:latest`).

The rig is intentionally a thin wrapper that defers to:

- `mini-swe-agent`'s own CLI (`mini-extra swebench`) for inference, and
- `SWE-bench`'s own harness (`python -m swebench.harness.run_evaluation`) for grading.

So any bug fix in those upstream tools is picked up automatically.

## Repository layout (target)

```
.
├── README.md
├── PLAN.md                        # this file
├── pyproject.toml                 # PEP 621 metadata; pulls deps for both repos
├── uv.lock                        # generated on first `uv sync`
├── .env.example                   # all knobs, with sane defaults
├── .gitignore
├── config/
│   ├── mini-swe-agent.local.yaml  # mini-swe-agent config wired to local LLM
│   └── litellm-registry.json      # cost-tracking stub for the local model
├── scripts/
│   ├── setup.sh                   # create venv, install both repos
│   ├── check_server.py            # probe LLM, auto-resolve LLM_MODEL
│   ├── pull_images.sh             # pull & retag ghcr.io/epoch-research images
│   ├── _mswea_image_patch.py      # 6-line monkey-patch loaded via PYTHONSTARTUP
│   ├── run_inference.sh           # run mini-swe-agent batch
│   ├── run_evaluation.sh          # run SWE-bench harness grading
│   └── report.py                  # summarise the eval JSON outputs
└── runs/                          # all run artifacts (gitignored)
    ├── smoke-<timestamp>/
    │   ├── inference/             # mini-swe-agent preds.json + trajectories
    │   ├── evaluation/            # swebench harness logs + report
    │   └── report.txt             # human-readable summary
    └── …
```

## High-level flow

```
   ┌──────────────────┐    ┌──────────────────┐    ┌────────────────────┐
   │  Local LLM       │◀───│  mini-swe-agent  │───▶│  preds.json        │
   │  (OpenAI-compat) │    │  (batch)         │    │  + .traj.json files│
   └──────────────────┘    └──────────────────┘    └─────────┬──────────┘
   ┌──────────────────┐    ┌──────────────────┐              │
   │ ghcr.io/epoch-   │───▶│  SWE-bench       │◀─────────────┘
   │ research images  │    │  harness         │──▶ report.json + logs
   └──────────────────┘    └──────────────────┘
```

## Key design decisions (user-confirmed)

| Decision | Choice | Rationale |
|---|---|---|
| Python env | `uv` venv + `uv pip` | Used by mini-swe-agent quickstart; fastest |
| Image registry | Pull from `ghcr.io/epoch-research`, retag to SWE-bench names | No upstream forks; works with both mini-swe-agent and SWE-bench harness |
| Default scope | 5-instance smoke test | Cheap sanity check; bump via `SWEBENCH_SLICE` |
| Cost tracking | `MSWEA_COST_TRACKING=ignore_errors` | Local LLM has no cost; avoids litellm registry gymnastics |

## Image-name compatibility (the one annoying bit)

The two projects name images differently:

| Project | Image-key pattern |
|---|---|
| mini-swe-agent `swebench.py` | `docker.io/swebench/sweb.eval.x86_64.<id>:latest`, with `__` → `_1776_` |
| `ghcr.io/epoch-research` (epoch blog) | `ghcr.io/epoch-research/sweb-bench.eval.x86_64.<id>:latest`, `__` preserved |
| SWE-bench harness | `sweb.eval.<arch>.<id>:latest` (namespace-prefixed), `__` → `_1776_` |

Strategy:

1. `docker pull ghcr.io/epoch-research/sweb-bench.eval.x86_64.<id>:latest`
2. `docker tag ... sweb.eval.x86_64.<id_with_1776>:latest`
3. Run SWE-bench harness with `--namespace none` so it uses the local tag.
4. For mini-swe-agent, prepend a tiny 6-line wrapper script that monkey-patches
   `minisweagent.run.benchmarks.swebench.get_swebench_docker_image_name` to
   return the ghcr.io name, then calls `main()`.

## Per-file plan

### `pyproject.toml`

PEP 621 project that pins two source deps and one extra:

```toml
[project]
name = "swebench-local-rig"
version = "0.1.0"
requires-python = ">=3.11"
dependencies = [
    "mini-swe-agent @ git+https://github.com/SWE-agent/mini-swe-agent.git",
    "swebench       @ git+https://github.com/SWE-bench/SWE-bench.git",
    "datasets>=2.20",
    "litellm>=1.55",
    "openai>=1.50",   # for the local-server health probe
    "python-dotenv",
    "rich",
    "typer",
]
```

`uv sync` then installs both repos editable (or pinned to a ref via `uv.lock`).

### `scripts/setup.sh`

```bash
#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
uv venv .venv --python 3.11
# shellcheck disable=SC1091
source .venv/bin/activate
uv pip install -e .
echo "✓ venv ready at .venv — run 'source .venv/bin/activate' to enter"
```

### `.env.example` (and runtime `.env`)

```dotenv
# === Local LLM ===
# Port 1234 is the default port for LM Studio; vLLM defaults to 8000, Ollama
# to 11434. Whatever it is, the rig auto-detects the model id from /v1/models
# at the top of every run, so you usually do NOT need to set LLM_MODEL by hand.
LLM_BASE_URL=http://10.77.0.2:1234/v1
LLM_API_KEY=lm-studio              # LM Studio ignores the value, but litellm
                                   # still requires a non-empty bearer token
# LLM_MODEL is auto-resolved by check_server.py from GET /v1/models. If your
# server returns more than one model, set it explicitly:
# LLM_MODEL=openai/<id-from-/v1/models>
LLM_MAX_CONTEXT=131072             # advertised to litellm for context-window checks

# === Run scope ===
SWEBENCH_SUBSET=verified
SWEBENCH_SPLIT=test
SWEBENCH_SLICE=0:5                 # default: 5-instance smoke test
SWEBENCH_WORKERS=4
SWEBENCH_RUN_ID=smoke-$(date +%Y%m%d-%H%M%S)

# === Agent limits (override mini-swe-agent defaults) ===
AGENT_STEP_LIMIT=250
AGENT_COST_LIMIT=3.0
```

### `config/mini-swe-agent.local.yaml`

Extends the upstream `swebench.yaml` (we reference it by file name so the
merge happens automatically via `-c`):

```yaml
agent:
  step_limit: ${AGENT_STEP_LIMIT}
  cost_limit: ${AGENT_COST_LIMIT}

environment:
  environment_class: docker
  cwd: /testbed
  timeout: 120                 # a bit more lenient for local slow models

model:
  model_name: ${LLM_MODEL}
  cost_tracking: ignore_errors
  model_kwargs:
    drop_params: true
    temperature: 0.0
    api_base: ${LLM_BASE_URL}
    api_key: ${LLM_API_KEY}
```

(Loaded with `mini-extra swebench -c swebench.yaml -c config/mini-swe-agent.local.yaml …` —
the second `-c` overrides without dropping the upstream prompt template.)

### `config/litellm-registry.json`

Stub so litellm's cost path has *some* entry (only used because `cost_tracking`
is `ignore_errors`; this is belt-and-suspenders):

```json
{
  "<your-model-name>": {
    "max_tokens": 131072,
    "input_cost_per_token": 0.0,
    "output_cost_per_token": 0.0,
    "litellm_provider": "openai",
    "mode": "chat"
  }
}
```

### `scripts/check_server.py`

Tiny OpenAI-compat probe — calls `GET {base}/models`, prints the model id,
and **auto-resolves `LLM_MODEL`** when the user has not set it explicitly.
This is the source of truth for the model id used downstream by litellm.

```python
#!/usr/bin/env python3
"""Probe the local OpenAI-compat LLM and (optionally) resolve LLM_MODEL."""
import os, sys, urllib.request, json

base = os.environ["LLM_BASE_URL"].rstrip("/")
hdrs = {"Authorization": f"Bearer {os.environ['LLM_API_KEY']}"}
try:
    with urllib.request.urlopen(urllib.request.Request(f"{base}/models",
                                                       headers=hdrs),
                                timeout=10) as r:
        data = json.load(r)
        ids = [m["id"] for m in data.get("data", [])]
except Exception as e:
    print(f"✗ cannot reach {base}/models: {e}", file=sys.stderr); sys.exit(1)

print(f"✓ {base} serves {len(ids)} model(s): {ids[:5]}"
      + ("…" if len(ids) > 5 else ""))
if not ids:
    print("✗ server reports zero models", file=sys.stderr); sys.exit(1)

# Resolve LLM_MODEL for downstream scripts if not already set
if "LLM_MODEL" not in os.environ or not os.environ["LLM_MODEL"]:
    chosen = ids[0]
    os.environ["LLM_MODEL"] = f"openai/{chosen}"
    # Persist for the current shell via a sourced companion
    with open(".env.last_resolved", "w") as f:
        f.write(f"LLM_MODEL=openai/{chosen}\n")
    print(f"  → auto-resolved LLM_MODEL=openai/{chosen} (first model listed)")
elif not os.environ["LLM_MODEL"].startswith("openai/"):
    # litellm requires a provider prefix for the OpenAI-compat adapter
    os.environ["LLM_MODEL"] = f"openai/{os.environ['LLM_MODEL']}"
```

### `scripts/pull_images.sh`

Pulls the required epoch-research images and re-tags them so the SWE-bench
harness finds them locally with `--namespace none`. Iterates over the same
instance list that the inference step will use (resolved from a
`--subset/--split/--slice` query up front so the two steps are in sync).

```bash
#!/usr/bin/env bash
set -euo pipefail
SUBSET="${SWEBENCH_SUBSET:-verified}"
SPLIT="${SWEBENCH_SPLIT:-test}"
SLICE="${SWEBENCH_SLICE:-0:5}"
ARCH="x86_64"
NAMESPACE="ghcr.io/epoch-research"

# Resolve the list of instance_ids we'll actually run
IDS=$(uv run --no-sync python -c "
from datasets import load_dataset
ds = load_dataset('princeton-nlp/SWE-bench_Verified', split='$SPLIT')
start, stop = (int(x) if x else None for x in '$SLICE'.split(':'))
for inst in ds[start:stop]:
    print(inst['instance_id'])
")

echo "Will pre-stage ${IDS}:"
echo "$IDS" | sed 's/^/  - /'
echo "$IDS" | while read -r id; do
  [[ -z "$id" ]] && continue
  src="${NAMESPACE}/sweb-bench.eval.${ARCH}.${id,,}:latest"
  compat_id="${id//__/_1776_}"
  dst="sweb.eval.${ARCH}.${compat_id}:latest"
  docker pull "$src"
  docker tag "$src" "$dst"
done
echo "✓ all images staged locally"
```

### `scripts/run_inference.sh` (the heart of inference)

Invokes the upstream `mini-extra swebench` entry point, with one small monkey-patch
applied via a thin wrapper so that the script pulls the ghcr.io image directly
(no need to pre-tag for inference, only for evaluation):

```bash
#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
# shellcheck disable=SC1091
source .venv/bin/activate
set -a; source .env; set +a
RUN_DIR="runs/${SWEBENCH_RUN_ID}/inference"
mkdir -p "$RUN_DIR"

# The env var tells the wrapper which registry prefix to use for the docker image.
export MSWEA_IMAGE_REGISTRY="ghcr.io/epoch-research/sweb-bench.eval.x86_64"

# mini-extra reads the upstream config + our override
mini-extra swebench \
  -c swebench.yaml \
  -c config/mini-swe-agent.local.yaml \
  --subset "$SWEBENCH_SUBSET" \
  --split  "$SWEBENCH_SPLIT" \
  --slice  "$SWEBENCH_SLICE" \
  --output "$RUN_DIR" \
  --workers "$SWEBENCH_WORKERS" \
  --model   "$LLM_MODEL"
```

The wrapper that handles the image override is a single 6-line Python file at
`scripts/_mswea_image_patch.py` (created by `setup.sh` if missing) that
`run_inference.sh` injects via `PYTHONSTARTUP` — no fork of mini-swe-agent
required:

```python
import os
if os.getenv("MSWEA_IMAGE_REGISTRY"):
    from minisweagent.run.benchmarks import swebench as _m
    _prefix = os.environ["MSWEA_IMAGE_REGISTRY"]
    def _patched(instance):
        if instance.get("image_name") or instance.get("docker_image"):
            return _m.get_swebench_docker_image_name(instance)
        return f"{_prefix}.{instance['instance_id']}:latest"
    _m.get_swebench_docker_image_name = _patched
```

### `scripts/run_evaluation.sh`

Hands the just-generated `preds.json` to the upstream SWE-bench harness. Because
`pull_images.sh` has already retagged the epoch images to the harness's local
naming scheme, the harness can be told `--namespace none` to avoid trying to
build anything and just use what is on disk.

**`--cache_level env`** (user-confirmed) means: keep the `base` and `env`
Docker images on disk between runs so subsequent instances reuse them via
Docker's layer cache, but throw away the per-instance `sweb.eval.x86_64.<id>`
images after each run. This is exactly the trade-off the
[epoch blog](https://epoch.ai/latest/swebench-docker) recommends — it cuts
disk usage by an order of magnitude (≈30 GiB for the full Verified set)
versus `--cache_level instance`, while still letting the epoch images be
reused across runs. With 5-instance smoke tests you will not even notice
the difference; for the full 500 it saves you ~100+ GB.

```bash
#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
source .venv/bin/activate
set -a; source .env; set +a
INFER_DIR="runs/${SWEBENCH_RUN_ID}/inference"
EVAL_DIR="runs/${SWEBENCH_RUN_ID}/evaluation"
mkdir -p "$EVAL_DIR"
cd "$EVAL_DIR"

# Convert preds.json (dict[instance_id -> {model_patch, ...}]) to preds.jsonl,
# which is what swebench.harness.utils expects.
python -c "
import json, sys
d = json.load(open('../inference/preds.json'))
for v in d.values():
    print(json.dumps(v))
" > preds.jsonl

python -m swebench.harness.run_evaluation \
  --dataset_name    princeton-nlp/SWE-bench_Verified \
  --split           "$SWEBENCH_SPLIT" \
  --predictions_path preds.jsonl \
  --run_id          "$SWEBENCH_RUN_ID" \
  --namespace       none \
  --cache_level     env \
  --max_workers     "$SWEBENCH_WORKERS" \
  --timeout         1800
```

> See the next section for a detailed walk-through of what `--namespace none`
> and `--cache_level env` actually do inside the harness.

## What is the "SWE-bench harness grading step"?

This rig is a two-stage pipeline. The **first stage** is *inference* — the
LLM proposes a patch for every problem instance. The **second stage** is
*grading* (the part that the SWE-bench harness does), and it is conceptually
different from inference: the model is no longer involved at all. We are
mechanically checking whether the LLM's proposed patch actually fixes the
issue, using the project's own test suite.

The harness (`swebench.harness.run_evaluation`) does this for one
`SWEbenchInstance` at a time. The full algorithm is in
`swebench/harness/run_evaluation.py:run_instance`; in plain English it is:

1. **Build / start a container.** Given an instance spec, decide which
   image to use:
   - With `--namespace <ns>` (default `swebench`) it pulls
     `<ns>/sweb.eval.<arch>.<id>:latest` from a remote registry.
   - With `--namespace none` it uses the *local* image
     `sweb.eval.<arch>.<id>:latest`, which is exactly what
     `pull_images.sh` has prepared.
   The container is started with the repo checked out at the pre-PR
   commit, in `/testbed`, with all dependencies installed (the `env`
   image baked in `conda activate testbed` for Python repos).

2. **Apply the model's patch.** Copy `preds.jsonl`'s `model_patch` into
   the container as `/tmp/patch.diff` and try to apply it with `git apply`
   (or `patch -p1` as a fallback). If all three strategies fail, the
   instance is graded as *patch apply failed* and counts as ✗.

3. **Apply the gold test patch.** This is the "secret sauce" of SWE-bench:
   the *project maintainers'* test changes (from the PR that fixed the
   bug) are applied on top, so the test suite is now the version that
   *would have* caught the bug.

4. **Run the eval script.** The harness ships, per instance, a small
   shell script (`eval_script_list`) that runs only the tests relevant
   to this issue — both the `FAIL_TO_PASS` tests (which were failing
   before the fix and must now pass) and the `PASS_TO_PASS` tests
   (which were passing before and must still pass — a regression
   guard). It captures stdout/stderr, exit code, and wall-clock.

5. **Grade.** A log parser (per language, in
   `swebench/harness/log_parsers/`) maps test runner output to a
   `PASSED`/`FAILED` verdict. The instance is `resolved = True` iff
   every `FAIL_TO_PASS` test passed *and* every `PASS_TO_PASS` test
   still passes. That's the per-instance score.

6. **Tear down.** Stop the container, write `report.json` and
   `test_output.txt` into `logs/run_evaluation/<run_id>/<model>/<id>/`,
   and — depending on `--cache_level` — optionally `docker rmi` the
   per-instance image.

After the loop, `make_run_report` aggregates all `report.json` files into
the headline metric: **`% resolved` over the 500 instances in SWE-bench
Verified**. That single number is what people publish in benchmark
leaderboards.

### What `--namespace none` means in this rig

By default the harness builds (or downloads) its own images. With
`--namespace none` we tell it "don't try to build, don't try to pull
from a registry — the image is already on this machine under the
short name `sweb.eval.<arch>.<id>:latest`." That's the name that
`pull_images.sh` produced via `docker tag` from the epoch-research
images. The harness's image key is built in
`swebench/harness/test_spec/test_spec.py:TestSpec.instance_image_key` —
note the `__` → `_1776_` substitution.

### What `--cache_level env` means

After each per-instance container exits, the harness decides what to
delete based on the cache level:

| `--cache_level` | `base` image | `env` image | `sweb.eval.<id>` instance image |
|---|---|---|---|
| `none` | deleted | deleted | deleted |
| `base` | kept | deleted | deleted |
| **`env`** (ours) | **kept** | **kept** | **deleted** |
| `instance` | kept | kept | kept |

The epoch-research setup is specifically built for `--cache_level env`:
the `env` images are the shared, expensive ones (apt installs, conda
environments, huge pip wheels), so caching them pays off massively;
the per-instance images are small and cheap to re-pull from
`ghcr.io/epoch-research` if you re-run. This is how Epoch fits all 500
Verified instances in ~30 GiB and runs the whole benchmark in ~62
minutes.

### `scripts/report.py`

Parses the harness's final `evaluation_results/` JSON and the
`logs/run_evaluation/<run_id>/.../report.json` files; prints:

- `% resolved` (the headline SWE-bench Verified number)
- per-instance exit status counts
- top-5 errors with stack-trace excerpts
- wall-clock + token usage from `preds.json` trajectories

The body of the script is roughly:

For completeness, the body of the script is roughly:

```python
#!/usr/bin/env python3
"""Aggregate the per-instance report.json files from swebench's run_evaluation."""
import json, sys, glob
from pathlib import Path

run_dir = Path(sys.argv[1])
log_root = run_dir / "evaluation" / "logs" / "run_evaluation" / run_dir.name
reports = list(log_root.glob("*/report.json"))

resolved = total = 0
status_counts: dict[str, int] = {}
for rp in reports:
    d = json.loads(rp.read_text())
    for inst_id, r in d.items():
        total += 1
        status_counts[r.get("status", "?")] = status_counts.get(r.get("status", "?"), 0) + 1
        if r.get("resolved"): resolved += 1

print(f"=== {run_dir.name} ===")
print(f"resolved: {resolved}/{total}  ({resolved/total:.1%})")
for s, c in sorted(status_counts.items(), key=lambda kv: -kv[1]):
    print(f"  {s:<20s} {c}")
```

### Top-level `run.sh` (entry point)

```bash
#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
export PYTHONSTARTUP="$(pwd)/scripts/_mswea_image_patch.py"

# 1) one-time
[[ -d .venv ]] || ./scripts/setup.sh
set -a; source .env; set +a

# 2) health
uv run --no-sync python scripts/check_server.py

# 3) pull + retag images for the chosen slice
./scripts/pull_images.sh

# 4) inference
./scripts/run_inference.sh

# 5) evaluation
./scripts/run_evaluation.sh

# 6) summarise
uv run --no-sync python scripts/report.py "runs/${SWEBENCH_RUN_ID}"
```

## End-to-end command recap

```bash
# 1. (one time) initialize venv & install both repos
./scripts/setup.sh
cp .env.example .env && $EDITOR .env     # set LLM_BASE_URL, LLM_MODEL, etc.

# 2. (per run) the whole pipeline
./run.sh

# 3. (optional) bump the smoke test to a real eval
SWEBENCH_SLICE=0:500 SWEBENCH_WORKERS=8 SWEBENCH_RUN_ID=full-$(date +%s) ./run.sh
```

## What this rig explicitly does **not** do

- It does **not** modify the upstream `mini-swe-agent` or `SWE-bench` repos.
  All overrides go through `-c` config flags, environment variables, or a
  6-line monkey-patch on `PYTHONSTARTUP`.
- It does **not** rebuild Docker images. Everything runs against
  pre-built, registry-pulled, re-tagged images.
- It does **not** re-host the LLM. The user is expected to point
  `LLM_BASE_URL` at a running OpenAI-compatible server (vLLM, llama.cpp
  `--server`, Ollama, LM Studio, etc.).

## Implementation sequence (ordered, each step is testable on its own)

User-confirmed: **local grading is mandatory** (step 7). No `sb-cli`
substitution, no cloud fallback — the harness is always run locally
inside the rig, with `--namespace none --cache_level env`.

Each step ends with a one-liner you can run to verify the step works
before moving on. Steps are deliberately ordered so a failure at step
N cannot waste time spent on steps N+1..M.

### 1. Project skeleton & metadata

Create the empty layout, write `pyproject.toml` with the two source deps
(`mini-swe-agent` and `swebench` from Git) and supporting packages
(`datasets`, `litellm`, `openai`, `python-dotenv`, `rich`, `typer`).
Also write `.gitignore` (ignore `.venv/`, `runs/`, `.env`,
`.env.last_resolved`, `__pycache__/`, `*.traj.json`) and `.env.example`
with the values from this plan.

**Verify:** `ls` shows the layout from the diagram; `cat .env.example`
matches.

### 2. Python environment (uv venv + install)

Write `scripts/setup.sh` and run it. `uv` creates `.venv/`, `uv pip
install -e .` resolves and installs both upstream repos plus deps.

**Verify:** `.venv/bin/python -c "import minisweagent, swebench"` exits 0.

### 3. LM Studio probe + model auto-resolver

Write `scripts/check_server.py`, copy `.env.example` → `.env`, then run
it against the user's `http://10.77.0.2:1234/v1`. The script hits
`GET /v1/models`, prints the list, and writes
`LLM_MODEL=openai/<first-id>` to `.env.last_resolved`.

**Verify:** the script prints e.g. `✓ http://10.77.0.2:1234/v1 serves
1 model(s): ['llama-3.1-8b-instruct']` and writes
`.env.last_resolved`.

### 4. litellm registry stub

Write `config/litellm-registry.json` with the entry for the model id
resolved in step 3 (replace `<your-model-name>` with the actual id
returned by LM Studio, or write the script that edits this in place
when the model changes). `LLM_MAX_CONTEXT` should also be tuned to the
model's real context window.

**Verify:** `uv run python -c "import litellm, json;
litellm.utils.register_model(json.load(open('config/litellm-registry.json')))"
` exits 0.

### 5. mini-swe-agent local config + image-override patch

Write `config/mini-swe-agent.local.yaml` (extending the upstream
`swebench.yaml`) and `scripts/_mswea_image_patch.py` (the 6-line
`PYTHONSTARTUP` monkey-patch). The patch is loaded by setting
`PYTHONSTARTUP` in `run.sh`.

**Verify:** manually invoke mini-swe-agent in single-instance mode
against a known easy instance:

```bash
export PYTHONSTARTUP="$(pwd)/scripts/_mswea_image_patch.py"
mini-extra swebench-single \
  -c swebench.yaml -c config/mini-swe-agent.local.yaml \
  --subset verified --split test -i 0 --model "$LLM_MODEL"
```

If this produces a `last_swebench_single_run.traj.json` with a
`COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT` line, the LLM plumbing works.

### 6. Pull + retag Docker images

Write `scripts/pull_images.sh` and run it. For the 5 default instance
ids it `docker pull`s from `ghcr.io/epoch-research/sweb-bench.eval.x86_64.<id>:latest`
and `docker tag`s them to the SWE-bench harness's local naming
convention. (The first run downloads ~3-5 GB per Django instance; this
is the dominant one-time cost.)

**Verify:** `docker images | grep sweb.eval.x86_64` lists the
5 retagged images, and one of them can be started interactively:
`docker run --rm -it sweb.eval.x86_64.django_1776_django-11099:latest
bash`.

### 7. Run inference (mini-swe-agent batch)

Write `scripts/run_inference.sh` and run it standalone. It activates
the venv, exports `MSWEA_IMAGE_REGISTRY`, and calls
`mini-extra swebench` with both upstream and local `-c` configs.

**Verify:** `runs/${SWEBENCH_RUN_ID}/inference/preds.json` exists and
has 5 entries, each with a non-empty `model_patch` (or, for failures,
the `exit_status` field present).

### 8. Run local grading (SWE-bench harness)

Write `scripts/run_evaluation.sh` and run it. Converts
`preds.json` → `preds.jsonl`, then invokes
`python -m swebench.harness.run_evaluation` with
`--namespace none --cache_level env` against the *same* SWE-bench
Verified dataset and the same slice. The harness starts one container
per instance, applies the patch, runs the gold tests, writes a
per-instance `report.json`, and finally a top-level report.

This is the **local grading** step the user wants — it runs entirely
on the user's machine, no external API calls beyond the local LLM
(which is no longer involved at this stage).

**Verify:** `runs/${SWEBENCH_RUN_ID}/evaluation/logs/run_evaluation/<run_id>/<model>/<instance_id>/report.json`
exists for every instance in the slice, and each contains a
`resolved: true|false` field.

### 9. Report aggregator

Write `scripts/report.py`. It walks the per-instance `report.json`
files emitted in step 8 and prints the headline SWE-bench Verified
metric: `resolved: N/5 (NN.N%)` plus per-status counts.

**Verify:** running it against the run from step 8 prints the correct
`% resolved` (verifiable manually with `jq '.resolved' evaluation/logs/run_evaluation/.../report.json`).

### 10. End-to-end smoke test via `run.sh`

Write the top-level `run.sh` that chains setup → check_server →
pull_images → run_inference → run_evaluation → report. Run it from a
clean state (after `rm -rf .venv runs/ && docker rmi -f $(docker images
-q sweb.eval.x86_64*)`).

**Verify:** total runtime is on the order of *minutes × workers* (most
time is spent in step 7 + 8; for 5 Django instances on a local LM
Studio GPU expect ~5-15 min on a single worker); the final `report.txt`
matches step 9.

### 11. Document the rig

Write a `README.md` that mirrors this PLAN.md but is user-facing: how
to start LM Studio, how to load a model that supports tool/function
calling (mini-swe-agent requires it), the one-time `./scripts/setup.sh`,
and the per-run `./run.sh`. Reference the "Decisions confirmed in this
session" section for the actual values to put in `.env`.

**Verify:** a fresh reader can clone → setup → run in 3 commands.

## Decisions confirmed in this session

- `LLM_BASE_URL=http://10.77.0.2:1234/v1` (port 1234 = LM Studio's default).
- `LLM_MODEL` is **not** hard-coded — `check_server.py` probes
  `GET /v1/models`, prints the list, auto-picks the first one, and
  exports `LLM_MODEL=openai/<that-id>` for downstream scripts. (The
  `openai/` prefix is the litellm provider hint for any OpenAI-compatible
  endpoint; the `LM_API_KEY=lm-studio` placeholder is fine because LM
  Studio ignores the token's value but litellm still requires the
  header.)
- `LLM_MAX_CONTEXT=131072` (advertised to litellm for context-window
  checks; tune to match the actual model).
- `--cache_level env` (user-confirmed): keep the shared `base` and
  `env` Docker images on disk between runs; discard the per-instance
  `sweb.eval.x86_64.<id>` images. Recommended by the epoch blog
  (cuts disk usage by an order of magnitude vs. `instance`).
- Scope default: 5-instance smoke test (`SWEBENCH_SLICE=0:5`); bump
  with `SWEBENCH_SLICE=0:500` for the full Verified set.
- Cost tracking: `MSWEA_COST_TRACKING=ignore_errors` and litellm
  registry stub for the local model (zero-cost).

## Still open (worth surfacing if needed)

1. The `litellm-registry.json` stub uses a fixed `max_tokens=131072`.
   If the local model has a different context window, edit it.
2. The `mini-extra swebench` agent's default `cost_limit: 3.0` is
   USD-denominated; with cost tracking off it has no effect, so the
   only effective cap is `step_limit: 250` (≈250 LLM turns per
   instance). Bump via `AGENT_STEP_LIMIT` if a model tends to spiral.
3. If at any point you'd rather not run the local harness grading
   step and instead use the cloud-based `sb-cli` (which submits
   `preds.json` to a hosted runner), the rig's `run_evaluation.sh`
   is the only piece you'd replace — the inference stage is
   unchanged.
