#!/usr/bin/env bash
# Builds each eio/* branch's main bundle and runs the harness twice per branch.
set -u
LOG=/tmp/opencode/signal-eio
cd ~/src/Signal-Desktop || exit 1
for branch in eio/harness eio/opt1-stdio-guard eio/opt2-guarded-reporter eio/opt3-dialog-dedupe; do
  echo "=== $branch ==="
  git checkout -q "$branch" || { echo "checkout FAILED $branch"; continue; }
  if ! pnpm run build:rolldown:prod > "$LOG/build-$(basename $branch).log" 2>&1; then
    echo "build FAILED $branch (see $LOG/build-$(basename $branch).log)"; continue
  fi
  for run in r1 r2; do
    SETTLE=30 OBSERVE=45 python3 "$LOG/harness.py" "$(basename $branch)-$run" \
      >> "$LOG/matrix.jsonl" 2>> "$LOG/harness.err" \
      || echo "harness FAILED $branch-$run"
  done
done
echo "MATRIX_DONE"
