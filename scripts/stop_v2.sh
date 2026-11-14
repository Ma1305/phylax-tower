#!/usr/bin/env bash
# Stop what scripts/run_v2.sh started (serve and its children, and setup_search if still running).
set -uo pipefail
cd "$(dirname "$0")/.."
LOGS="${HOLDSHORT_LOGS:-logs/v2}"
tree() { local p; for p in $(pgrep -P "$1" 2>/dev/null); do tree "$p"; done; echo "$1"; }
stop_pid() {  # pidfile
  local f="$1" pid all
  [[ -f "$f" ]] || return 0
  pid="$(cat "$f")"
  if kill -0 "$pid" 2>/dev/null; then
    # uv run -> python -m holdshort.serve -> one process per worker: signal the whole tree,
    # or the workers outlive serve when uv goes first
    all="$(tree "$pid")"
    kill -TERM $all 2>/dev/null || true
    for _ in $(seq 1 15); do
      local alive=0 p; for p in $all; do kill -0 "$p" 2>/dev/null && alive=1; done
      [[ $alive -eq 0 ]] && break; sleep 1
    done
    kill -KILL $all 2>/dev/null || true
    echo "stop_v2: stopped pid $pid ($(basename "$f" .pid))"
  fi
  rm -f "$f"
}
stop_pid "$LOGS/serve.pid"
stop_pid "$LOGS/setup_search.pid"
