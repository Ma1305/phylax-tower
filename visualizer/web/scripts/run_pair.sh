#!/usr/bin/env bash
# Seed a scratch database, start the visualizer API and the built web app on free ports
# (18000-18999), run scripts/pair.mjs against them, stop everything and drop the database.
#   scripts/run_pair.sh [--shots]     DB: holdshort_v2_t<task>_pair (override with PAIR_DB)
set -euo pipefail
cd "$(dirname "$0")/.."
TASK=$(git rev-parse --abbrev-ref HEAD | sed -n 's#^hq/\([0-9]*\)-.*#\1#p')
DB=${PAIR_DB:-holdshort_v2_t${TASK:-0}_pair}
free() { for p in $(seq "$1" 18999); do ss -ltn | grep -q ":$p " || { echo "$p"; return; }; done; }
API_PORT=$(free 18380); WEB_PORT=$(free $((API_PORT + 1))); DEAD_PORT=$(free $((WEB_PORT + 1)))
cleanup() {
  kill "${API_PID:-}" "${WEB_PID:-}" 2>/dev/null || true
  wait 2>/dev/null || true
  (cd ../api && MONGODB_DB=$DB uv run python ../web/scripts/poke.py drop) || true
}
trap cleanup EXIT
(cd ../api && uv run python seed_demo.py --db "$DB")
(cd ../api && MONGODB_DB=$DB exec uv run uvicorn app:app --port "$API_PORT" --log-level warning) & API_PID=$!
# the tower's /control/* API is not part of this check: point at a port nothing listens on
VITE_VIZ_API=http://localhost:$API_PORT VITE_TOWER_API=http://localhost:$DEAD_PORT npm run build >/dev/null
node node_modules/vite/bin/vite.js preview --port "$WEB_PORT" --strictPort >/dev/null 2>&1 & WEB_PID=$!
for _ in $(seq 60); do curl -sf "localhost:$API_PORT/viz/health" >/dev/null && curl -sf "localhost:$WEB_PORT" >/dev/null && break; sleep 0.5; done
MONGODB_DB=$DB node scripts/pair.mjs "http://localhost:$WEB_PORT" "http://localhost:$API_PORT" "$@"
