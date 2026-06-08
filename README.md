# SWE-Bench Verified rig for a local OpenAI-compatible LLM

End-to-end scripted rig that uses the upstream
[mini-swe-agent](https://github.com/SWE-agent/mini-swe-agent) and
[SWE-bench](https://github.com/SWE-bench/SWE-bench) tools to verify a
local OpenAI-compatible LLM (LM Studio, vLLM, Ollama, llama.cpp, etc.)
on the
[SWE-bench Verified](https://huggingface.co/datasets/princeton-nlp/SWE-bench_Verified)
benchmark, against the optimized Docker images from
[epoch.ai/latest/swebench-docker](https://epoch.ai/latest/swebench-docker)
(`ghcr.io/epoch-research/sweb-bench.eval.x86_64.<id>:latest`).

The rig is a thin wrapper that defers to the upstream `mini-extra swebench`
CLI for inference and `swebench.harness.run_evaluation` for grading. No
upstream code is modified.

See `PLAN.md` for the design and rationale.

## Layout

```
.
├── pyproject.toml, requirements.txt     # project metadata + deps
├── .env.example                         # all knobs, with sane defaults
├── config/
│   ├── mini-swe-agent.local.yaml        # mini-swe-agent config wired to local LLM
│   └── litellm-registry.json            # cost-tracking stub for the local model
├── scripts/
│   ├── setup.sh                         # create venv, install both repos
│   ├── check_server.py                  # probe LLM, auto-resolve LLM_MODEL
│   ├── pull_images.sh                   # pull & retag ghcr.io/epoch-research images
│   ├── _mswea_image_patch.py            # 6-line monkey-patch loaded via PYTHONSTARTUP
│   ├── run_inference.sh                 # run mini-swe-agent batch
│   ├── run_evaluation.sh                # run SWE-bench harness local grading
│   └── report.py                        # summarise the eval JSON outputs
├── vendor/swebench/                     # swebench editable install (one-time clone)
├── runs/                                # all run artifacts (gitignored)
└── run.sh                               # end-to-end entry point
```

## Quick start

```bash
# 0. Prerequisites: uv, docker running, and a local LLM on http://10.77.0.2:1234/v1
#    (LM Studio's default port; change LLM_BASE_URL in .env for vLLM/Ollama/...).
#    The model must support tool/function calling (mini-swe-agent requires it).

# 1. (one time) create venv and install both upstream repos
./scripts/setup.sh

# 2. configure
cp .env.example .env
$EDITOR .env                 # adjust LLM_BASE_URL, LLM_API_KEY, etc. if needed

# 3. (per run) the whole pipeline
./run.sh
```

The default scope is a 5-instance smoke test (`SWEBENCH_SLICE=0:5`).
Bump to a real eval with (note: keep `SWEBENCH_WORKERS=1` — the local LLM
server is single-threaded, parallel requests just queue):

```bash
SWEBENCH_SLICE=0:100 SWEBENCH_RUN_ID=full-100-$(date +%s) ./run.sh
```

## What `./run.sh` does

1. **probe LLM** — `scripts/check_server.py` calls `GET {LLM_BASE_URL}/models`
   and auto-resolves `LLM_MODEL=openai/<first-id>`. The result is written
   to `.env.last_resolved` and exported to subsequent steps.
2. **pull + retag images** — `scripts/pull_images.sh` resolves the
   instance IDs for the chosen slice, then for each one runs
   `docker pull ghcr.io/epoch-research/sweb-bench.eval.x86_64.<id>:latest`
   and `docker tag … sweb.eval.x86_64.<id_with_1776>:latest` so the
   SWE-bench harness can find them locally with `--namespace none`.
3. **inference** — `scripts/run_inference.sh` calls
   `mini-extra swebench` with the upstream config + our local override
   (`config/mini-swe-agent.local.yaml`) and a 6-line `PYTHONSTARTUP`
   monkey-patch that redirects docker pulls to the ghcr.io images.
   Output: `runs/<run_id>/inference/preds.json` + per-instance
   `.traj.json` files.
4. **local grading** — `scripts/run_evaluation.sh` invokes
   `python -m swebench.harness.run_evaluation` with
   `--namespace none --cache_level env` against the same
   SWE-bench Verified dataset and the same slice. The harness
   applies the model patch, then the gold test patch, runs the
   project's own test suite, and writes a per-instance `report.json`.
5. **report** — `scripts/report.py` aggregates every
   `report.json` and prints / writes
   `runs/<run_id>/report.txt` with the headline
   `resolved: N/M (NN.N%)` number.

## Configuration

`.env` (all optional — defaults match `.env.example`):

| Var | Default | Purpose |
|---|---|---|
| `LLM_BASE_URL` | `http://10.77.0.2:1234/v1` | OpenAI-compat endpoint |
| `LLM_API_KEY` | `lm-studio` | Any non-empty string; LM Studio ignores the value |
| `LLM_MODEL` | (auto from `/v1/models`) | e.g. `openai/deepseek-v4-flash` |
| `LLM_MAX_CONTEXT` | `131072` | advertised to litellm for context checks |
| `SWEBENCH_SUBSET` | `verified` | dataset name shorthand |
| `SWEBENCH_SPLIT` | `test` | dataset split |
| `SWEBENCH_SLICE` | `0:5` | Python slice of instance indices |
| `SWEBENCH_WORKERS` | `4` | parallel workers |
| `SWEBENCH_RUN_ID` | `smoke-<timestamp>` | output directory name |
| `AGENT_STEP_LIMIT` | `250` | LLM turns per instance |
| `AGENT_COST_LIMIT` | `3.0` | ignored (cost tracking off) |

## Image registry strategy

| Project | Image key |
|---|---|
| mini-swe-agent | `docker.io/swebench/sweb.eval.x86_64.<id>:latest`, `__`→`_1776_` |
| `ghcr.io/epoch-research` | `…/sweb-bench.eval.x86_64.<id>:latest`, `__` preserved |
| SWE-bench harness | `sweb.eval.<arch>.<id>:latest`, `__`→`_1776_` |

We use `docker pull` + `docker tag` so both tools see a consistent local
naming scheme; the SWE-bench harness is then run with
`--namespace none` (use what's on disk, don't try to build/pull from
a registry). With `--cache_level env` we keep the expensive
shared base/env images between runs and discard the per-instance
image afterwards — that's the exact trade-off the
[epoch blog](https://epoch.ai/latest/swebench-docker) recommends
(≈30 GiB for the full Verified set; ≈62 min on a 32-core machine).

## Prerequisites

- macOS or Linux
- [`uv`](https://docs.astral.sh/uv/) ≥ 0.11
- Docker (Desktop on macOS, or dockerd on Linux) with the daemon running
  and enough disk for the chosen slice (≈2-5 GB per Django instance,
  ≈30 GiB for the full 500-instance Verified set)
- A local OpenAI-compatible LLM server reachable at `LLM_BASE_URL`,
  serving a model that supports tool/function calling

## Notes on the `swebench` vendored copy

`uv pip install` from a git source drops the
`swebench/harness/constants/fixtures/*.Cargo.lock` files (the upstream
`pyproject.toml` doesn't list them under `[tool.setuptools.package-data]`).
The rig clones `SWE-bench` to `vendor/swebench/` and installs it
editable, which makes the fixtures accessible at runtime via
`resources.files(swebench.resources).joinpath(...)`. Re-running
`./scripts/setup.sh` is idempotent; the clone only happens once.
