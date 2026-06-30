#!/usr/bin/env bash
# Campground watcher cron wrapper. Runs every 5 min. LLM-free.
# A single run takes ~13 min (WA occupancy cross-check), longer than the 5-min
# timer interval, so guard against overlapping runs with an flock. If a previous
# run is still going, this invocation exits immediately (non-blocking lock).
set -euo pipefail
export PATH="$HOME/.local/bin:/run/current-system/sw/bin:/usr/bin:/bin"
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$DIR"
LOCK="$DIR/.run.lock"
exec 9>"$LOCK"
if ! flock -n 9; then
  echo "[$(date -Is)] previous run still active; skipping this tick" >> "$DIR/cron.log"
  exit 0
fi
exec uv run --python 3.12 watch.py >> "$DIR/cron.log" 2>&1
