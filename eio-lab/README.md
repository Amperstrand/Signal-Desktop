# eio-lab — reproducible evidence for the orphaned-stdio EIO dialog loop

Everything behind our analysis in
[signalapp/Signal-Desktop#7644](https://github.com/signalapp/Signal-Desktop/issues/7644)
(and #7057): the instrumented harness, the fd-routing diagnostics, the raw
run artifacts, and the full evaluation with every addendum.

## What each file is

| File | Purpose |
|---|---|
| `harness.py` | Main harness: launches the dev build on a sacrificial PTY (fds only, no ctty — replicates `signal-desktop &` + terminal close), readiness-gated on an instrumentation beacon, orphans the slave by closing the master (EIO on writes, no SIGHUP), fires a deterministic trigger, counts handleError/dialog/exit from file-based instrumentation |
| `stream_iso.py` | fd-routing matrix: routes stdin/stdout/stderr independently (PTY vs file vs /dev/null) and watches the electron MAIN pid — proves no partial redirection mitigates the death |
| `run_matrix.sh` | Drives build + 2 runs per branch across the option set |
| `EVALUATION.md` | Full write-up: method, pre-registered predictions, results, scoring, and four addenda (combined branch, harness env-drift, fd-matrix, fix scope-check) |
| `runs/` | Raw artifacts per run: `events.jsonl` (instrumentation events + ms timestamps), `summary.json`, `master.log` (what the app wrote to the PTY before it died) |

## Branches this lab exercises

On this fork: `eio/harness` (instrumented control), `eio/opt1-stdio-guard`,
`eio/opt2-guarded-reporter`, `eio/opt3-dialog-dedupe`, `eio/opt23-combined`.
The instrumentation commit is shared across all of them (env-gated by
`SIGNAL_EIO_INSTR=1`, fully inert otherwise). Clean PR candidates:
`pr/dialog-dedupe`, `pr/dialog-dedupe-plus-guard`.

## Reproducing

```bash
git clone -b eio/harness https://github.com/Amperstrand/Signal-Desktop
cd Signal-Desktop && pnpm install && pnpm run generate
bash eio-lab/run_matrix.sh          # builds each branch's bundle, 2 runs each
python3 eio-lab/stream_iso.py A     # fd-routing variants: A B D E, control
```

Scripts are published **as-run** — absolute paths (`~/src/Signal-Desktop`,
`/tmp/opencode/signal-eio`) are unchanged from the runs that produced
`runs/`, for provenance. Adjust to taste.

## Caveats (also in EVALUATION.md)

- The failure mode is environment-unstable: identical code presented as a
  Node-level dialog storm (2026-11-11), a silent `exit(0)` (option 1), and a
  hook-less zombie death (2026-11-13/14). The `runs/` timestamps show which
  mode each artifact belongs to.
- Dev build only; `--storage-profile` was silently ignored, runs used the
  `Signal-development` profile (never account-linked — logs carry no
  account data).
- Dialogs in this environment returned immediately rather than blocking;
  error multiplicity and incomplete-exit behavior are what's replicated,
  not the literal multi-hour modal loop.

> Provenance note: hostname in run artifacts was scrubbed (`x280` -> `LOCALHOST`)
> before publishing; no other modifications to raw evidence.
