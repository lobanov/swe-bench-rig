---
name: swebench-failure-analysis
description: "Turn completed swe-bench-rig runs into a per-instance failure-analysis HF dataset: stage trajectories/patches/test-outputs, classify failures deterministically, deep-read every trajectory via sub-agents to assign root causes, verify harness/environment false-negatives, and publish to Hugging Face. Use when the user wants to analyse SWE-bench run failures, understand why instances failed, build/refresh the failure dataset, or append new runs to it. Run dirs live under runs/<run_id>/ with inference/ and evaluation/ subdirs."
---

# SWE-bench failure analysis → HF dataset

Produce a per-instance failure-analysis dataset from one or more `runs/<run_id>/` dirs and
publish it to Hugging Face. The pipeline is **idempotent and appendable**: re-running with all
run dirs regenerates the table deterministically; `hf upload` commits only the diff.

Three scripts in `scripts/` do the mechanical work; the **one step that needs an agent** is the
deep per-instance trajectory read (step 3). Everything else is deterministic.

## Prerequisites

- One or more completed run dirs: `runs/<run_id>/inference/<iid>/<iid>.traj.json`,
  `runs/<run_id>/inference/preds.json`, and graded
  `runs/<run_id>/evaluation/logs/run_evaluation/*/*/<iid>/{report.json,test_output.txt}`.
  (Grade with `scripts/run_evaluation.sh` first if `evaluation/` is missing.)
- `.venv` present; `hf auth whoami` works for upload.

## Pipeline

### 1 & 2 — Stage artifacts + build the table

```bash
.venv/bin/python scripts/make_failure_dataset.py --out hf-dataset \
    --arch Metal \
    --population <sample|prior-failures> \
    --server-commit <ds4_sha> \
    --server-url https://github.com/antirez/ds4/commit/<ds4_sha> \
    runs/<run_id> [runs/<run_id> ...]          # pass EVERY run, old and new
```

This stages (sanitized) run-scoped artifacts — `trajectories/<run_id>/<iid>.traj.json`,
`transcripts/<run_id>/<iid>.md` (rendered via `render_transcript.py`), `patches/…`,
`test_outputs/…` — and writes the `instances` subset (`instances.jsonl` + `instances.csv`). It also
writes a GitHub-pasteable `RESULTS_TABLE.md` to the repo root (a LOCAL convenience that duplicates
`instances.jsonl`; `--table <path>` to relocate, `--table ''` to skip) — **do not upload it to the
dataset.** `--population` labels the passed runs (e.g. an ordinary slice is `sample`, a curated
known-failure slice is `prior-failures`); merge-preserve keeps other runs' labels. It joins each
trajectory with the harness `report.json` to assign a deterministic `failure_category`:

- `resolved` · `empty_patch` (no diff) · `patch_apply_failed` · `regression` (PASS_TO_PASS
  failures > 0) · `unfixed` (target still fails, broke nothing) · `limits` · `other`.

It prints a **`NEED DIAGNOSIS`** list of any `uid` lacking `diagnoses/<run_id>/<iid>.json`.
Sanitization (dummy `lm-studio` api_key, `/Users/dain` home path) is automatic.

### 3 — Deep per-instance diagnosis (sub-agent pass) — the agent step

For every `uid` in `NEED DIAGNOSIS`, read its `transcripts/<run_id>/<iid>.md`,
`patches/<run_id>/<iid>.diff`, `test_outputs/<run_id>/<iid>.txt`, and the row in
`instances.jsonl`, then write `diagnoses/<run_id>/<iid>.json`.

**Parallelize**: partition instances into ~10 balanced groups by transcript size and launch one
`general-purpose` sub-agent per group (each writes one JSON per instance). Keep total transcript
size per agent ≲250 KB so context stays bounded.

The grader gives the *outcome*; the diagnosis is the qualitative *why*. Required schema (valid
JSON, no comments):

```json
{
  "instance_id": "<iid>",
  "root_cause_primary": "resolved|misdiagnosed_problem|wrong_location|incomplete_fix|over_broad_fix|test_expectation_gap|gave_up_early|looping_wasted_steps|ran_out_of_time|empty_submission|env_or_tooling|other",
  "root_cause_secondary": "<same vocab or null>",
  "near_miss": true,
  "confidence": "high|medium|low",
  "what_model_did": "1-2 sentences: the actual approach it took",
  "failure_summary": "2-4 sentences SPECIFIC to this instance: why the test failed (or why it worked)",
  "evidence": ["concrete observations with step numbers / quotes from transcript/patch/test output"]
}
```

Instruct agents to be concrete: cite step numbers, the file/function edited, and what the failing
test expected vs what the patch did (read `test_outputs/` for the exact assertion). For
`resolved`, set `root_cause_primary:"resolved"` and characterise the winning approach.

### 4 — VERIFY harness/environment false-negatives (critical, don't skip)

Some "failures" are **broken test images, not model errors** — especially astropy (old astropy +
modern toolchain). Before trusting the numbers, inspect `test_outputs/` for any failure where the
patch looks correct or a *whole* test module failed. Tell-tale signs:

- `collected 0 items / N errors` / `Interrupted: N errors during collection` — env, not patch.
- `using nose-specific method: setup(self)` — modern pytest rejects old nose-style setup.
- `LooseVersion(...)`, `np.int`/`np.bool` removals, `ImportError` at collection — toolchain drift.
- A *total wipeout* (e.g. 0/80 PASS_TO_PASS) almost always means collection/setup failed for all
  tests regardless of the patch. Distinguish from a real regression (some pass, some fail) and from
  the model breaking an import it added (that one IS the model's fault — check the traceback).

When confirmed, add the `instance_id` + reason to `HARNESS_FALSE_NEGATIVES` in
`scripts/make_failure_dataset.py`. Those rows get `harness_false_negative=true` and are credited in
`resolved_adjusted`. Report both raw and adjusted resolve rates.

### 5 — Re-build, then upload

```bash
# re-run the SAME step-1 command to fold diagnoses into the table
.venv/bin/python scripts/make_failure_dataset.py --out hf-dataset --arch Metal \
    --server-commit <sha> --server-url <url> runs/<run_id> ...

hf upload <namespace>/<dataset> hf-dataset . --type dataset [--private] \
    --commit-message "..."
# first creation: add --private for a private repo.
# if you RESTRUCTURED paths, add --delete "*" to remove remote orphans in the same commit.
# CONTRIBUTOR (no write access / wants review): add --create-pr to open a PR on the
#   dataset for the owner to review and merge, instead of pushing to main.
```

**Record findings in the journal, keep the README evergreen.** Add a dated, append-only entry to
`hf-dataset/FINDINGS.md` with the snapshot for this data: which runs, per-`population` resolve rates
(raw + adjusted), taxonomy counts, harness false-negatives, and divergence stats. Do NOT bake those
numbers into `README.md` — it describes what the dataset is and how it's built (structure, fields,
populations concept, methodology), which doesn't change as runs are appended. Only touch the README
if the *structure/schema* changes.

## Appending a new run later

You do **not** need the original operator's run dirs, but you **do** need the existing dataset
content — the per-instance `diagnoses/` are sub-agent outputs that live only in the published
repo and are **not regenerable** from run dirs. So pull the dataset first.

**Contributor flow (no write access to the dataset — submit for review):**

```bash
# 0. pull the existing dataset (brings old rows, artifacts, and diagnoses)
hf download <namespace>/<dataset> --type dataset --local-dir hf-dataset

# 1. grade your new run, then build passing ONLY your new run dir(s).
#    The driver preserves every row whose run_id is already in instances.jsonl
#    but wasn't passed — old runs are kept verbatim, not dropped.
scripts/run_evaluation.sh                      # for your new run
.venv/bin/python scripts/make_failure_dataset.py --out hf-dataset --arch <arch> \
    --server-commit <sha> --server-url <url> runs/<your-new-run>

# 2. diagnose the NEED DIAGNOSIS uids (sub-agents) → diagnoses/<run_id>/<iid>.json
# 3. re-run the same build command, then open a PR for review:
hf upload <namespace>/<dataset> hf-dataset . --type dataset --create-pr \
    --commit-message "Append run <your-new-run>"
```

`hf upload --create-pr` prints a PR URL; the dataset owner reviews and merges it.

**Owner flow (write access, has all run dirs):** pass every run dir (old + new) to rebuild the
full table from scratch, then `hf upload` directly (no `--create-pr`). Equivalent result — the
merge-preserve path just lets you skip having the old run dirs locally.

Run-scoped paths (`<kind>/<run_id>/<iid>`) mean re-running the same instance in a new run never
overwrites; `instances.jsonl` is sorted by `uid` so an append is a clean additive diff.

## Comparing repeated instances (temperature-0 divergence)

If the same `instance_id` was run in 2+ runs, comparing those runs shows how
(non-)deterministic the stack is. The rig samples at **temp 0**, but a real server
is not bit-deterministic (batching, MoE routing, non-associative float reductions on
GPU/Metal), so runs fork. This is high-value: it separates harness reproducibility
from server nondeterminism.

### Deterministic check (no model calls)

```bash
.venv/bin/python scripts/compare_repeats.py runs/<a> runs/<b> [runs/<c> ...] \
    --out REPEATS_REPORT.md
```

It finds instances completed in >1 run and reports, per instance: `identical_command_prefix`
(how many leading shell commands matched across runs — **this is also the 0-based index of the
first command that differs**, so `0` means the runs forked at the very first action),
`commands_identical` (true only when the full command sequences matched, i.e. no behavioural fork),
whether the **outcome** diverged (resolved / empty / failed), `patch_identical`, and patch
similarity (difflib ratio). Writes `REPEATS_REPORT.md` +
`.jsonl`. Observed on this rig: runs typically **fork at action #0** (immediate
divergence), yet sometimes reconverge to a near-identical patch and sometimes don't —
so report both behavioural fork point *and* outcome/patch convergence, they tell
different stories.

> **Gotcha that invalidates naive diffs:** mini-swe-agent drives the model via
> tool-calls, so the agent's action is in
> `tool_calls[0].function.arguments.command` and its thinking in `reasoning_content`
> — `message.content` is **empty**. A trajectory diff over `content` sees everything
> as identical. Always compare the command sequence (compare_repeats.py does).

### Qualitative pass (sub-agent) — for diverging or outcome-flipping pairs

For each interesting instance (prioritise `outcome_diverges=true`, then low
`patch_similarity`), launch one sub-agent with this prompt:

> Two temperature-0 runs of the SAME SWE-bench instance `<iid>` diverged. Read both
> transcripts (`transcripts/<run_id>/<iid>.md` for each run) and both patches
> (`patches/<run_id>/<iid>.diff`), plus the divergence record for this instance in
> `REPEATS_REPORT.jsonl`. The runs use the same model at temp 0, so any difference is
> server/sampling nondeterminism. Determine: (1) at what point and in what way the
> approaches first diverged (commands AND reasoning); (2) whether they reconverged on
> the same fix or pursued genuinely different strategies; (3) for outcome differences
> (one resolved/empty/failed, the other not), the specific decision or step that
> tipped it — e.g. one run ran the failing test and iterated while the other submitted
> blind, or one got stuck in a loop and ran out of steps. Write
> `diagnoses_divergence/<iid>.json` with `{instance_id, fork_point, fork_description,
> reconverged (bool), strategy_delta, outcome_tipping_point, confidence}`. Be concrete
> with step numbers and quotes.

Synthesize the structured outputs into a short "reproducibility" section: what fraction
fork immediately, how often outcomes flip, and whether flips trace to a few decision
points (iterate-vs-submit-blind, loop-and-time-out) rather than the whole trajectory.

## Key columns in `instances.jsonl`

`uid` (`<run_id>/<instance_id>`), `repo`, `arch`, `server_build_commit/url`, `resolved`,
`resolved_adjusted`, `harness_false_negative(+_reason)`, `failure_category`, `exit_status`,
patch stats, `fail_to_pass_*`/`pass_to_pass_*`, token/time/`cache_hit_rate` metrics, and the
qualitative `root_cause_primary/secondary`, `near_miss`, `confidence`, `what_model_did`,
`failure_summary`, `evidence`, plus `*_path` pointers to each artifact.

## Gotchas

- **Empty patches aren't graded** — the harness skips them (no `report.json`/`test_output.txt`);
  they're correctly `pass=False`. Distinguish `ran_out_of_time` (Timeout) from `empty_submission`.
- **Denominator**: the harness reports resolved over *graded* (excludes empty patches). For a fair
  rate over the whole batch, count empty patches as failures (n includes them here).
- `hf upload` may print "0 committed" while still creating a commit — verify with
  `hf datasets info <id>` (check `siblings` for the expected paths).
- Visibility is a repo setting; re-uploads can surprise you — confirm `private` with
  `hf datasets info`, and fix with `hf repos settings <id> --private/--public --type dataset`.
