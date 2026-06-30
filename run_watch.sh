#!/usr/bin/env bash
# Campground watcher single-tick runner for cron / systemd. LLM-free.
# A full run can take ~10-13 min (the WA occupancy cross-check is the slow part),
# so if you run this on a short interval, guard against overlapping runs with an
# flock: if a previous tick is still going, this one exits immediately.
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$DIR"
LOCK="$DIR/.run.lock"
exec 9>"$LOCK"
if ! flock -n 9; then
  echo "[$(date -Is)] previous run still active; skipping this tick" >> "$DIR/cron.log"
  exit 0
fi
# watch.py has a uv shebang (PEP 723), so this resolves its own deps.
exec ./watch.py >> "$DIR/cron.log" 2>&1