# Instance-id input lists

Newline-delimited SWE-bench Verified instance IDs for the `SWEBENCH_INPUT_FILE` mode
(consumed by `scripts/pull_images.sh` → `--input-file`). Drop a list here and run:

```bash
SWEBENCH_INPUT_FILE=inputs/<your-list>.txt \
SWEBENCH_RUN_ID=<run-name> \
./run.sh
```

The lists themselves are **gitignored** — they're experiment-specific (e.g. a curated set of
prior failures). Only this README is tracked, so the directory persists in a clean checkout.
