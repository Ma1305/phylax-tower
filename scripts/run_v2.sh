#!/usr/bin/env bash
# Start the whole tower in the background:  scripts/run_v2.sh [--db NAME] [--port 8000]
# 1. scripts/setup_atlas.py (collections, validators, world seed)
# 2. scripts/setup_search.py in the background (Atlas Search / Vector indexes; slow)
# 3. python -m holdshort.serve in the background; logs in logs/v2/, pid in logs/v2/serve.pid
# 4. waits for GET /health (status ok). Stop with scripts/stop_v2.sh.
set -euo pipefail
cd "$(dirname "$0")/.."
DB="${MONGODB_DB:-holdshort_v2}"
PORT=8000
while [[ $# -gt 0 ]]; do
  case "$1" in
    --db) DB="$2"; shift 2 ;;
    --port) PORT="$2"; shift 2 ;;
    *) echo "usage: $0 [--db NAME] [--port N]" >&2; exit 2 ;;
  esac
done
LOGS="${HOLDSHORT_LOGS:-logs/v2}"
mkdir -p "$LOGS"
PIDFILE="$LOGS/serve.pid"
if [[ -f "$PIDFILE" ]] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
  echo "run_v2: already running (pid $(cat "$PIDFILE")); scripts/stop_v2.sh first" >&2
  exit 1
fi
echo "run_v2: setup_atlas --db $DB"
uv run python scripts/setup_atlas.py --db "$DB" > "$LOGS/setup_atlas.log" 2>&1 \
  || { echo "run_v2: setup_atlas failed, see $LOGS/setup_atlas.log" >&2; tail -20 "$LOGS/setup_atlas.log" >&2; exit 1; }
nohup uv run python scripts/setup_search.py --db "$DB" > "$LOGS/setup_search.log" 2>&1 &
echo $! > "$LOGS/setup_search.pid"
export MONGODB_DB="$DB"
nohup uv run python -m holdshort.serve --db "$DB" --port "$PORT" --host 127.0.0.1 \
  > "$LOGS/serve.log" 2>&1 &
echo $! > "$PIDFILE"
echo "run_v2: serve pid $(cat "$PIDFILE"), db $DB, port $PORT, log $LOGS/serve.log"
for _ in $(seq 1 90); do
  if body="$(curl -fsS "http://127.0.0.1:$PORT/health" 2>/dev/null)"; then
    if [[ "$body" == *'"status":"ok"'* ]]; then echo "run_v2: healthy: $body"; exit 0; fi
  fi
  kill -0 "$(cat "$PIDFILE")" 2>/dev/null || { echo "run_v2: serve exited" >&2; tail -30 "$LOGS/serve.log" >&2; exit 1; }
  sleep 1
done
echo "run_v2: /health not ok after 90 s: ${body:-no answer}" >&2
exit 1
