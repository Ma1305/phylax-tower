#!/usr/bin/env bash
# Stop what scripts/run_v2.sh started (serve and its children, and setup_search if still running).
set -uo pipefail
cd "$(dirname "$0")/.."
LOGS="${HOLDSHORT_LOGS:-logs/v2}"
stop_pid() {  # pidfile
  local f="$1" pid
  [[ -f "$f" ]] || return 0
  pid="$(cat "$f")"
  if kill -0 "$pid" 2>/dev/null; then
    # uv run -> python: signal the whole tree below the pid
    local kids; kids="$(pgrep -P "$pid" || true)"
    kill -TERM "$pid" $kids 2>/dev/null || true
    for _ in $(seq 1 15); do kill -0 "$pid" 2>/dev/null || break; sleep 1; done
    kill -0 "$pid" 2>/dev/null && kill -KILL "$pid" $kids 2>/dev/null
    echo "stop_v2: stopped pid $pid ($(basename "$f" .pid))"
  fi
  rm -f "$f"
}
stop_pid "$LOGS/serve.pid"
stop_pid "$LOGS/setup_search.pid"
