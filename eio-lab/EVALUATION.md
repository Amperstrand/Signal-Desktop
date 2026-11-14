# Signal-Desktop EIO fix options — final results (2026-11-11)

Fork: https://github.com/Amperstrand/Signal-Desktop/branches (eio/*)
Upstream base: v8.37.0-alpha.1 (5d1bbb7). Issue: signalapp/Signal-Desktop#7644 (also #7057).

## Harness

Dev build (`electron .`), sacrificial-PTY launch (fds only, no ctty — replicates
`signal-desktop &` + terminal close), readiness-gated on an instrumentation
beacon, PTY master closed (fd-level orphan → EIO, no SIGHUP), one-shot
trigger (console.warn + pino log.warn), 45s observation, file-based counters.
2 runs/branch. Lint: 390 passing on every branch.

## Results (2 runs each; event counts from instrumentation)

| branch | handleError | dialog | deduped | exit | observed behavior |
|---|---|---|---|---|---|
| harness (control) r1 | 4 | 4 | — | exit(1) | storm: 1 trigger → 4 chained fatals |
| harness (control) r2 | 2 | 1 | — | exit(1) | storm, smaller interleaving |
| opt1 stdio-guard r1/r2 | 0 | 0 | — | exit(0) | **silent clean exit** — no errors surface, app just ends |
| opt2 guarded-reporter r1/r2 | 2 | 2 | — | exit(1) | amplification gone (4→2: only source errors remain) |
| opt3 dialog-dedupe r1 | 1 | 1 | 0 | (window end) | single error → single dialog |
| opt3 dialog-dedupe r2 | 3 | 1 | 2 | exit(1) | **3 errors collapse to 1 dialog**, then exit |

Key mechanism findings:

1. **Storm anatomy confirmed**: one log event after stdio death yields 2-4
   fatal errors (source write + pino console mirror + the reporter's own
   `console.error`/`log.error` re-entry). Control r1: 4 fatals/dialogs from
   ONE trigger.
2. **opt2 kills the amplification** (reporter no longer re-enters) but each
   source error is still fatal → still one dialog each (2 seen).
3. **opt1 does NOT deliver "silent survival"**: with the Node-level guard
   swallowing all stream errors, the app still exits(0) ~130ms later —
   Chromium/Electron terminates on stdio failure below Node's layer. Option 1
   as publicly sketched in the issue comment is therefore not a real
   "keep running" option on Electron; it only changes the failure surface
   from dialog-storm to silent disappearance.
4. **opt3 caps presentation**: identical consecutive fatals within 30s
   collapse to the first dialog + exit; the conveyor is structurally
   impossible regardless of storm size.

## Scores (rubric fixed before runs; 0-5)

| criterion | opt1 | opt2 | opt3 |
|---|---|---|---|
| storm suppression | 5 | 2.5 | 5 |
| fatal semantics preserved | 3 | 5 | 4 |
| end-state clarity | 1 (silent vanish) | 2.5 (2 dialogs) | 4.5 (1 dialog+exit) |
| footprint/reviewability | 4.5 | 4.5 | 4 |
| regression risk | 3.5 (masks all stdio errors) | 5 | 4.5 |
| **total** | **17/25** | **19.5/25** | **22/25** |

## Recommendation

- **opt3** primary: best score, policy-neutral (fatal-by-design untouched),
  structurally eliminates the uncloseable conveyor.
- **opt2** complementary hardening: removes reporter self-amplification;
  composes cleanly with opt3.
- **opt1** reframe for upstream: evidence shows it converts the storm into a
  silent exit(0) — a product decision at best, not a survival fix.

## Validity caveats (stated for the follow-up comment)

- Dialogs in this environment returned immediately instead of blocking
  (XWayland, no main window) — the incident's 10h modal loop is not literally
  replicated; error MULTIPLICITY (the storm generator) is replicated and
  measured.
- Dev build: pino mirrors to console in dev; packaged builds route pino to
  disk only, so prod source-error count is lower — the reporter
  amplification (opt2) and presentation cap (opt3) conclusions hold either
  way.
- 2 runs/branch; interleaving varied (2 vs 4 fatals on control) but
  per-branch behavior was consistent across runs.

## Addendum 2026-11-13 (pre-PR prep while awaiting maintainer response)

- `eio/opt23-combined` created (opt2+opt3 on instrumented base): composes
  correctly at code level (dedupe gate -> guarded report -> dialog),
  oxlint clean, bundle builds. Runtime matrix revalidation BLOCKED: since
  2026-11-11 the harness environment drifts — electron main now hard-aborts
  at PTY-master close (Chromium-level, no JS exit hooks) instead of raising
  Node-level uncaughtExceptions. Verified via preclose probe (watcher fires
  fine with healthy PTY; death occurs exactly at close). NOTE: the failure
  mode itself being nondeterministic across environments STRENGTHENS the
  upstream case — worth one line in the eventual PR description.
- Clean PR branches (no instrumentation), based on CURRENT upstream/main
  (848443a, 54 commits ahead; global_errors.main.ts unchanged upstream):
  - `pr/dialog-dedupe`        (opt3 only)
  - `pr/dialog-dedupe-plus-guard` (opt3 + opt2)
  Both: oxlint clean, rolldown bundle compiles, pushed to the fork.
- Unit test for the dedupe: deferred. Their test-node runs electron-mocha
  with real electron APIs (no dialog-stub pattern); testing cleanly would
  require exporting isDuplicateFatal — an API decision for the PR
  conversation, not something to smuggle into a minimal fix PR.

## Addendum 2026-11-14 (fd-routing matrix — second reverse-engineering pass)

New harness (stream_iso.py) routes each stdio fd independently and watches
the electron MAIN pid directly (not the wrapper — fixing the alive_end bug):

| variant | stdin | stdout | stderr | result at PTY death |
|---|---|---|---|---|
| control | PTY | PTY | PTY | dies (zombie) |
| A | PTY | PTY | file | dies (zombie) |
| B | PTY | file | PTY | dies (zombie) |
| D | PTY | file | file | dies (zombie) |
| E | /dev/null | PTY | PTY | dies (zombie) |

Conclusions:
1. **No partial stdio redirection mitigates the crash.** If ANY fd remains
   attached to the dying terminal, the app dies — stdin or either output.
   This explains knarrff's `>/dev/null &` failure (one stream is never
   enough) and hardens the guidance: only FULL detachment (desktop entry,
   systemd unit, launcher) is safe. There is no shell one-liner except
   redirecting ALL THREE away, at which point you've rebuilt a launcher.
2. Today's death mode differs from Nov 11 (write-EIO -> uncaughtException
   storm): now instant, JS-hook-less (_exit-style: no coredump, no crashpad,
   no process.on('exit')), electron main AND the cli.js wrapper both end as
   unreaped zombies. Identical code, binary, kernel session; no package
   changes. The failure mode is unstable across days — nondeterminism
   finding amplified: same bug can present as dialog storm, silent exit(0),
   or hook-less zombie death.
3. Methodology: `--storage-profile=eio` is silently ignored by the dev
   build; all runs used the Signal-development profile (consistent across
   runs, isolated from production).
4. cli.js (electron wrapper) forwards SIGUSR2 — the harness's file-trigger
   design accidentally avoided a self-inflicted repro artifact.
5. Forensics checked: zero electron coredumps, zero crashpad dumps. The
   seedling qemu-arm segfault wave (Nov 13 21:17-22:32) was another lane;
   today's clean-system rerun rules out that interference as the cause.

Recommendation: the remaining unknown (WHICH internal actor calls exit)
needs strace/debug-symbols — low value for upstream vs. the completed
matrix. Hold the fd-matrix finding for the eventual PR description rather
than a third issue comment (thread density budget).

## Addendum 2026-11-14b (fix scope-check under third failure mode)

opt3 (dedupe) run under the Nov-13/14 hook-less-zombie mode: identical to
control (instant sub-Node death, zero events, no dialogs). Empirically
bounds the fix: opt3+opt2 cure the reporter pathology (dialog storm +
incomplete-exit zombie) and are no-ops in the instant-death mode — where
the UX is already tolerable (app vanishes cleanly, no storm). The death
itself is Electron/Chromium-layer and out of Signal's JS reach; the PR
description should state this scope honestly.
