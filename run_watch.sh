#!/usr/bin/env bash
# Compatibility launcher for cron/launchd. Locking and log rotation are handled
# portably by run_watch.py using Python's standard library.
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [[ -n "${CAMPWATCH_PYTHON:-}" ]]; then
  PYTHON_BIN="$CAMPWATCH_PYTHON"
else
  PYTHON_BIN=""
  for candidate in /opt/homebrew/bin/python3 /usr/local/bin/python3 /usr/bin/python3; do
    if [[ -x "$candidate" ]] && "$candidate" -c 'import sys; raise SystemExit(sys.version_info < (3, 10))'; then
      PYTHON_BIN="$candidate"
      break
    fi
  done
fi

if [[ -z "$PYTHON_BIN" || ! -x "$PYTHON_BIN" ]]; then
  echo "campground-watcher requires Python 3.10+; set CAMPWATCH_PYTHON to its absolute path" >&2
  exit 1
fi

exec "$PYTHON_BIN" "$DIR/run_watch.py"
