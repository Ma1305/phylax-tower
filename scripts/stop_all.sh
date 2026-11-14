#!/usr/bin/env bash
# Stop everything scripts/run_all.sh started: visualizer web, visualizer API, a looping feed
# (scripts/feed.sh --loop &), then the tower (scripts/stop_v2.sh).
set -uo pipefail
cd "$(dirname "$0")/.."
LOGS="${HOLDSHORT_LOGS:-logs/v2}"
tree() { local p; for p in $(pgrep -P "$1" 2>/dev/null); do tree "$p"; done; echo "$1"; }
stop_pid() {  # pidfile
  local f="$1" pid all
  [[ -f "$f" ]] || return 0
  pid="$(cat "$f")"
  if kill -0 "$pid" 2>/dev/null; then
    all="$(tree "$pid")"
    kill -TERM $all 2>/dev/null || true
    for _ in $(seq 1 10); do kill -0 "$pid" 2>/dev/null || break; sleep 0.5; done
    kill -KILL $all 2>/dev/null || true
    echo "stop_all: stopped pid $pid ($(basename "$f" .pid))"
  fi
  rm -f "$f"
}
stop_pid "$LOGS/feed.pid"
stop_pid "$LOGS/viz_web.pid"
stop_pid "$LOGS/viz_api.pid"
scripts/stop_v2.sh
