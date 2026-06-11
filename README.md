# SWE-Bench Verified rig for a local OpenAI-compatible LLM

End-to-end scripted rig that uses the upstream
[mini-swe-agent](https://github.com/SWE-agent/mini-swe-agent) and
[SWE-bench](https://github.com/SWE-bench/SWE-bench) tools to verify a
local OpenAI-compatible LLM (LM Studio, vLLM, Ollama, llama.cpp, etc.)
on the
[SWE-bench Verified](https://huggingface.co/datasets/princeton-nlp/SWE-bench_Verified)
benchmark, against the optimized Docker images from
[epoch.ai/latest/swebench-docker](https://epoch.ai/latest/swebench-docker)
(`ghcr.io/epoch-research/swe-bench.eval.x86_64.<id>:latest`).

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
│   ├── litellm-registry.json            # cost-tracking stub for the local model
│   └── sitecustomize.py                 # auto-loaded image-resolver monkey-patch
├── scripts/
│   ├── setup.sh                         # create venv, install both repos
│   ├── check_server.py                  # probe LLM, auto-resolve LLM_MODEL, write litellm registry
│   ├── sample_instances.py              # pick IDs: --slice M:N | --input-file | --n/--seed
│   ├── pull_images.sh                   # pull & retag ghcr.io/epoch-research images
│   ├── _render_yaml.py                  # substitute ${VAR} in mini-swe-agent.local.yaml
│   ├── run_inference.sh                 # run mini-swe-agent batch on the sample
│   ├── run_evaluation.sh                # run SWE-bench harness local grading
│   ├── run_to_csv.py                    # export per-instance metrics to results.csv
│   └── report.py                        # summarise results with Wilson 95% CI
├── config/
│   ├── mini-swe-agent.local.yaml        # mini-swe-agent config wired to local LLM
│   ├── litellm-registry.json            # cost-tracking stub, rewritten by check_server.py
│   └── sitecustomize.py                 # auto-loaded image-resolver patch + litellm silence
├── vendor/swebench/                     # swebench editable install (one-time clone)
├── runs/                                # all run artifacts (gitignored)
│   └── <run_id>/
│       ├── sampled_ids.txt              # the exact instances picked by the seed
│       ├── pull.log                     # docker pull + retag log
│       ├── inference.log                # mini-swe-agent run log
│       ├── eval.log                     # swebench harness grading log
│       ├── inference/                   # preds.json + per-instance .traj.json
│       ├── evaluation/                  # swebench harness logs + report
│       ├── report.txt                   # human-readable summary with CI
│       └── results.csv                  # per-instance metrics: pass, llm time, tokens
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

The default scope is a 5-instance smoke test sampled from the 500
SWE-bench Verified instances (random sample with `SWEBENCH_N=5` and
`SWEBENCH_SEED=1`). **Three mutually exclusive modes for picking
instances are supported:**

| Mode | How | When to use |
|---|---|---|
| **Contiguous slice** | `SWEBENCH_SLICE=M:N` (Python slice syntax; `:N` and `M:` work too) | Reproducible eval over a specific index range, e.g. `0:5` for smoke or `0:500` for full |
| **Explicit list** | `SWEBENCH_INPUT_FILE=path/to/ids.txt` (one ID per line) | Hand-curated set, e.g. only the Django bugs you care about |
| **Random sample** | `SWEBENCH_N=N` + `SWEBENCH_SEED=K` (default mode if the other two are empty) | Reproducible-but-arbitrary subset, e.g. for statistical sampling |

Bump to a real eval with (note: keep `SWEBENCH_WORKERS=1` — the local
LLM server is single-threaded, parallel requests just queue):

```bash
# Full benchmark (deterministic order, 0:500)
SWEBENCH_SLICE=0:500 SWEBENCH_RUN_ID=full-$(date +%s) ./run.sh

# Custom slice (e.g. instances 50..150)
SWEBENCH_SLICE=50:150 SWEBENCH_RUN_ID=slice-50-150-$(date +%s) ./run.sh

# Hand-picked list
echo -e "django__django-11999\nastropy__astropy-13033" > /tmp/my_ids.txt
SWEBENCH_INPUT_FILE=/tmp/my_ids.txt SWEBENCH_RUN_ID=custom-$(date +%s) ./run.sh

# Random sample (the original behavior)
SWEBENCH_N=100 SWEBENCH_SEED=1 SWEBENCH_RUN_ID=random-100-$(date +%s) ./run.sh
```

## What `./run.sh` does

1. **probe LLM** — `scripts/check_server.py` calls `GET {LLM_BASE_URL}/models`,
   auto-resolves `LLM_MODEL=openai/<first-id>`, and rewrites
   `config/litellm-registry.json` so litellm knows the model. The result
   is written to `.env.last_resolved` and exported to subsequent steps.
2. **sample instances** — `scripts/sample_instances.py` resolves
   which instance IDs to run, in one of three modes (mutually exclusive,
   `SWEBENCH_SLICE` wins first, then `SWEBENCH_INPUT_FILE`, then
   `SWEBENCH_N`/`SWEBENCH_SEED`):
   - `SWEBENCH_SLICE=M:N` (Python slice syntax; `:N` and `M:` also work)
     for a deterministic contiguous slice of the dataset.
   - `SWEBENCH_INPUT_FILE=path` for a hand-curated list of IDs (one per
     line; the script validates each against the dataset).
   - `SWEBENCH_N=N` + `SWEBENCH_SEED=K` for a reproducible random sample
     of N instances (the original default behavior).
   The chosen IDs are written to `runs/<run_id>/sampled_ids.txt`, one per
   line, in dataset order.
3. **pull + retag images** — `scripts/pull_images.sh` reads
   `sampled_ids.txt`, then for each ID runs
   `docker pull ghcr.io/epoch-research/swe-bench.eval.x86_64.<id>:latest`
   and `docker tag … sweb.eval.x86_64/<sweb.eval.x86_64.<id_1776>>:latest`
   so the SWE-bench harness can find them locally. Output logged to
   `runs/<run_id>/pull.log`.
4. **inference** — `scripts/run_inference.sh` renders
   `config/mini-swe-agent.local.yaml` (via `scripts/_render_yaml.py`) and
   calls `mini-extra swebench` with that config + a `sitecustomize.py`
   image-resolver monkey-patch (auto-loaded via `PYTHONPATH=config`).
   The `--filter` is built from `sampled_ids.txt`. Output:
   `runs/<run_id>/inference/preds.json` + per-instance `.traj.json`
   files; run log to `runs/<run_id>/inference.log`.
5. **local grading** — `scripts/run_evaluation.sh` invokes
   `python -m swebench.harness.run_evaluation` with
   `--namespace sweb.eval.x86_64 --cache_level env --instance_ids <sampled>`
   against the same SWE-bench Verified dataset. Instances with an
   existing `report.json` from a prior run are auto-skipped
   (so a re-invocation resumes instead of re-grading). Run log to
   `runs/<run_id>/eval.log`.
6. **report** — `scripts/report.py` aggregates every `report.json`,
    computes the Wilson 95% confidence interval on the resolved
    proportion, projects the expected score range on the full 500
    instances, and (when available) reports token totals from
    mini-swe-agent trajectories. Output goes to stdout and
    `runs/<run_id>/report.txt`.
7. **per-instance CSV** — `scripts/run_to_csv.py` emits
    `runs/<run_id>/results.csv` with one row per instance:
    `instance_id, pass, inference_time_sec, llm_time_sec,
    llm_calls_count, llm_prompt_tokens_count,
    llm_cached_prompt_tokens_count, llm_completion_tokens_count`.
    `pass` is read from the harness's `report.json` (instances the
    harness skipped, e.g. empty-patch, get `pass=False`).

## Configuration

`.env` (all optional — defaults match `.env.example`):

| Var | Default | Purpose |
|---|---|---|
| `LLM_BASE_URL` | `http://10.77.0.2:1234/v1` | OpenAI-compat endpoint |
| `LLM_API_KEY` | `lm-studio` | Any non-empty string; LM Studio ignores the value |
| `LLM_MODEL` | (auto from `/v1/models`) | e.g. `openai/deepseek-v4-flash` |
| `LLM_MAX_CONTEXT` | `131072` | advertised to litellm for context-window checks |
| `SWEBENCH_SUBSET` | `verified` | dataset name shorthand |
| `SWEBENCH_SPLIT` | `test` | dataset split |
| `SWEBENCH_SLICE` | (empty) | contiguous slice of the dataset, e.g. `0:5`, `50:150`, `0:`. Wins over `SWEBENCH_INPUT_FILE` and `SWEBENCH_N`. |
| `SWEBENCH_INPUT_FILE` | (empty) | path to a newline-delimited list of instance IDs. Validated against the dataset. |
| `SWEBENCH_N` | `5` | sample size for the random-sample mode (used only when both `SWEBENCH_SLICE` and `SWEBENCH_INPUT_FILE` are empty). |
| `SWEBENCH_SEED` | `1` | random seed (random-sample mode) |
| `SWEBENCH_WORKERS` | `1` | parallel workers (1 because the local LLM is single-threaded) |
| `SWEBENCH_RUN_ID` | `smoke` | output directory name under `runs/` |
| `AGENT_STEP_LIMIT` | `250` | LLM turns per instance (hard cap) |
| `AGENT_COST_LIMIT` | `3.0` | ignored (cost tracking off) |

## Reproducible sampling

`scripts/sample_instances.py` resolves the instance set in one of three
mutually-exclusive modes (see the "Configuration" table above):

- `--slice M:N` produces a deterministic contiguous slice (`ds[M:N]`).
- `--input-file path` reads newline-delimited instance IDs from a file
  and validates each against the dataset (unknown IDs abort with an
  error). Output is sorted in dataset order.
- `--n N --seed K` produces a reproducible random sample of N instances
  using `random.Random(K).sample(range(500), N)`.

In every mode, the chosen instance IDs are written to
`runs/<run_id>/sampled_ids.txt` in dataset order (not draw order), so
`pull_images.sh`, `run_inference.sh`, and `run_evaluation.sh` all consume
the same file. Re-running with the same `SWEBENCH_SLICE` (or the same
`SWEBENCH_N` + `SWEBENCH_SEED`) produces the same sample byte-for-byte.
The mode label is captured to `runs/<run_id>/sample.log` for post-mortem.

## Confidence interval and projection

`scripts/report.py` uses the [Wilson score interval](https://en.wikipedia.org/wiki/Binomial_proportion_confidence_interval)
for the resolved proportion (better-behaved than the normal approximation
for small N and proportions near 0 or 1), and then projects that interval
onto the full 500-instance SWE-bench Verified set. Example for 2/5
resolved:

```
resolved: 2/5  (40.0%)

95% confidence interval (Wilson score):
  observed: [ 11.8%,  76.9%]
  expected score on full SWE-bench Verified (500 instances):
    point estimate: 200/500  (40.0%)
    95% CI range:   59–384 / 500  (11.8%–76.9%)
```

The wide CI at N=5 is expected; the projection narrows quickly as N grows
(roughly ±10% at N=20, ±4% at N=100).

## Image registry strategy

| Project | Image key |
|---|---|
| mini-swe-agent (default) | `docker.io/swebench/sweb.eval.x86_64.<id>:latest`, `__`→`_1776_` |
| `ghcr.io/epoch-research` | `…/swe-bench.eval.x86_64.<id>:latest`, `__` preserved |
| SWE-bench harness (with `namespace` set) | `<namespace>/sweb.eval.<arch>.<id_with_1776>:latest` |

The rig uses a non-`None` namespace (`sweb.eval.x86_64`) so the harness
treats the per-instance image as remote and applies the `__`→`_1776_`
substitution. We then `docker pull` from `ghcr.io/epoch-research` and
`docker tag` to the namespaced local form
`sweb.eval.x86_64/sweb.eval.x86_64.<id_1776>:latest`, which is what the
harness then looks up. (With `--namespace none` the harness instead tries
to **build** the env images locally, which is not what we want on arm64
or any non-x86 host.)

With `--cache_level env` we keep the expensive shared base/env images
between runs and discard the per-instance image afterwards — that's the
exact trade-off the
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

### aarch64 / Apple-silicon note

The epoch-research Docker images are built for `linux/amd64` only. On an
`aarch64` host (Apple silicon, AWS Graviton, Raspberry Pi, etc.) Docker
will automatically run them through `qemu-user-static` emulation. Just
make sure the host has it installed and registered with binfmt_misc:

```bash
# Debian / Ubuntu
sudo apt-get install -y qemu-user-static

# macOS (Docker Desktop, OrbStack, Rancher Desktop): bundled, nothing to do

# Verify
docker run --rm --platform linux/amd64 alpine uname -m    # → x86_64
ls /proc/sys/fs/binfmt_misc | grep qemu                    # → qemu-x86_64 (and friends)
```

Emulation is transparent to the rig (no flags needed) but adds roughly
5–10× overhead per `docker exec` call. For fastest runs use a native
x86_64 host; the rig works identically on both.

## Notes on the `swebench` vendored copy

`uv pip install` from a git source drops the
`swebench/harness/constants/fixtures/*.Cargo.lock` files (the upstream
`pyproject.toml` doesn't list them under `[tool.setuptools.package-data]`).
The rig clones `SWE-bench` to `vendor/swebench/` and installs it
editable, which makes the fixtures accessible at runtime via
`resources.files(swebench.resources).joinpath(...)`. Re-running
`./scripts/setup.sh` is idempotent; the clone only happens once.
