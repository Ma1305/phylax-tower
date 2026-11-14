#!/usr/bin/env bash
# Start everything in the background:  scripts/run_all.sh [--db NAME]
#   tower            :8000  (scripts/run_v2.sh: setup_atlas, every tower process)
#   visualizer API   :8100  (visualizer/api, read-only)
#   visualizer web   :5180  (visualizer/web, vite dev server, VITE_* pointing at localhost)
# Logs and pid files are in logs/v2/ (HOLDSHORT_LOGS). Stop with scripts/stop_all.sh.
# Other ports (tests use 18000-18999): --tower-port N --viz-port N --web-port N.
set -euo pipefail
cd "$(dirname "$0")/.."
ROOT="$PWD"
DB="${MONGODB_DB:-holdshort_v2}"
TOWER_PORT=8000; VIZ_PORT=8100; WEB_PORT=5180
while [[ $# -gt 0 ]]; do
  case "$1" in
    --db) DB="$2"; shift 2 ;;
    --tower-port) TOWER_PORT="$2"; shift 2 ;;
    --viz-port) VIZ_PORT="$2"; shift 2 ;;
    --web-port) WEB_PORT="$2"; shift 2 ;;
    *) echo "usage: $0 [--db NAME] [--tower-port N] [--viz-port N] [--web-port N]" >&2; exit 2 ;;
  esac
done
export HOLDSHORT_LOGS="${HOLDSHORT_LOGS:-logs/v2}"
case "$HOLDSHORT_LOGS" in /*) LOGS="$HOLDSHORT_LOGS" ;; *) LOGS="$ROOT/$HOLDSHORT_LOGS" ;; esac
mkdir -p "$LOGS"
for name in viz_api viz_web; do
  f="$LOGS/$name.pid"
  if [[ -f "$f" ]] && kill -0 "$(cat "$f")" 2>/dev/null; then
    echo "run_all: $name already running (pid $(cat "$f")); scripts/stop_all.sh first" >&2; exit 1
  fi
done

scripts/run_v2.sh --db "$DB" --port "$TOWER_PORT"

echo "run_all: visualizer API on :$VIZ_PORT (db $DB)"
( cd visualizer/api
  uv sync -q >"$LOGS/viz_api.log" 2>&1
  MONGODB_DB="$DB" nohup uv run uvicorn app:app --host 127.0.0.1 --port "$VIZ_PORT" \
    >>"$LOGS/viz_api.log" 2>&1 </dev/null &
  echo $! > "$LOGS/viz_api.pid" )

echo "run_all: visualizer web on :$WEB_PORT"
( cd visualizer/web
  [[ -d node_modules ]] || npm install --no-audit --no-fund >"$LOGS/viz_web_install.log" 2>&1
  VITE_VIZ_API="http://localhost:$VIZ_PORT" VITE_TOWER_API="http://localhost:$TOWER_PORT" \
    nohup node node_modules/vite/bin/vite.js --host 127.0.0.1 --port "$WEB_PORT" --strictPort \
    >"$LOGS/viz_web.log" 2>&1 </dev/null &
  echo $! > "$LOGS/viz_web.pid" )

ok=0
for _ in $(seq 1 90); do
  if curl -fsS "http://127.0.0.1:$VIZ_PORT/viz/health" >/dev/null 2>&1 \
     && curl -fsS "http://127.0.0.1:$WEB_PORT/" >/dev/null 2>&1; then ok=1; break; fi
  sleep 1
done
if [[ $ok -ne 1 ]]; then
  echo "run_all: the visualizer did not come up; see $LOGS/viz_api.log and $LOGS/viz_web.log" >&2
  tail -5 "$LOGS/viz_api.log" "$LOGS/viz_web.log" >&2 || true
  exit 1
fi
cat <<MSG
run_all: everything is up (database $DB)
  web app          http://localhost:$WEB_PORT
  visualizer API   http://localhost:$VIZ_PORT/viz/health
  tower API        http://localhost:$TOWER_PORT/health
  feed             scripts/feed.sh incident    (TOWER_API=http://localhost:$TOWER_PORT)
  logs             $LOGS/{serve,viz_api,viz_web}.log
  stop             scripts/stop_all.sh
MSG
