#!/usr/bin/env bash
# Start the live Hold Short demo: action gate, safety/procedures/weather agents,
# API (uvicorn, :8000) and web UI (Vite dev server, :5173). Returns once everything
# is up; stop it all with scripts/demo_stop.sh.
#
#   scripts/demo.sh
#   API_PORT=8100 WEB_PORT=5273 scripts/demo.sh     # other ports (e.g. on a shared VM)
#
# - Database: MONGODB_DB from the .env (holdshort/env.py). scripts/setup_atlas.py runs
#   first (idempotent). Then every agent switch + the system switch is set ON by
#   "demo_setup" (logged), so a previous run's toggles can't break the beats.
#   KEEP_SWITCHES=1 skips that.
# - The listener runs inside the scenario run (text mode): Play in the UI calls
#   POST /scenario/start, which plays scenarios/laguardia/script.json in a thread of
#   the API process. HOLDSHORT_SCENARIO_SPEED (default 1 = real time, ~80 s).
# - Tier 2: scripts/setup_search.py (callsigns + incidents search indexes) runs in the
#   background (SKIP_SEARCH=1 skips it); the voice alert service (holdshort.voice_alert)
#   speaks executed STOPs with ElevenLabs when VOICE_ENABLED is true (text-only log otherwise).
# - Logs: logs/demo/<name>.log. PIDs (process groups): logs/demo/pids.
set -euo pipefail

cd "$(dirname "$0")/.."
API_PORT=${API_PORT:-8000}
WEB_PORT=${WEB_PORT:-5173}
LOG_DIR=logs/demo
PID_FILE=$LOG_DIR/pids
mkdir -p "$LOG_DIR"

if [ -s "$PID_FILE" ]; then
  echo "demo already running (see $PID_FILE); run scripts/demo_stop.sh first"; exit 1
fi
for port in "$API_PORT" "$WEB_PORT"; do
  if ss -ltnH "sport = :$port" 2>/dev/null | grep -q .; then
    echo "port $port is already in use; set API_PORT / WEB_PORT"; exit 1
  fi
done
rm -f "$LOG_DIR"/*.log
: > "$PID_FILE"

fail() {
  echo "FAIL: $1"
  if [ -n "${2:-}" ]; then tail -20 "$2"; fi
  scripts/demo_stop.sh || true
  exit 1
}

start_proc() {  # name command... (own session/process group so demo_stop can kill it all)
  local name=$1; shift
  setsid "$@" > "$LOG_DIR/$name.log" 2>&1 < /dev/null &
  echo "$! $name" >> "$PID_FILE"
}
wait_log() {  # name pattern timeout_s
  local i
  for ((i = 0; i < $3 * 10; i++)); do
    grep -q "$2" "$LOG_DIR/$1.log" 2>/dev/null && return 0
    sleep 0.1
  done
  fail "'$2' not seen in $LOG_DIR/$1.log within $3 s" "$LOG_DIR/$1.log"
}
wait_http() {  # url pattern timeout_s name
  local i
  for ((i = 0; i < $3 * 10; i++)); do
    curl -fsS "$1" 2>/dev/null | grep -q "$2" && return 0
    sleep 0.1
  done
  fail "$1 not ready within $3 s" "$LOG_DIR/$4.log"
}

DB_NAME=$(uv run python -c 'from holdshort.env import mongodb_db_name; print(mongodb_db_name())')
echo "== Atlas setup (database $DB_NAME)"
uv run python scripts/setup_atlas.py > "$LOG_DIR/setup.log" 2>&1 || fail "setup_atlas.py" "$LOG_DIR/setup.log"
if [ -z "${KEEP_SWITCHES:-}" ]; then
  for a in system listener weather safety procedures; do
    uv run python -m holdshort.gate set-permission "$a" on demo_setup >> "$LOG_DIR/setup.log" 2>&1 \
      || fail "set-permission $a" "$LOG_DIR/setup.log"
  done
  echo "   all switches ON (by demo_setup)"
fi

if [ ! -d web/node_modules ]; then
  echo "== npm install (web/)"
  (cd web && npm install --no-audit --no-fund > "../$LOG_DIR/npm_install.log" 2>&1) \
    || fail "npm install" "$LOG_DIR/npm_install.log"
fi

if [ -z "${SKIP_SEARCH:-}" ]; then
  echo "== Atlas Search setup in the background (logs/demo/search_setup.log)"
  start_proc search_setup uv run python scripts/setup_search.py --timeout 300
fi

echo "== starting gate + agents + voice alert"
start_proc gate uv run python -m holdshort.gate
start_proc safety uv run python -m holdshort.agents.safety_agent
start_proc procedures uv run python -m holdshort.agents.procedures_agent
start_proc weather uv run python -m holdshort.agents.weather_agent
start_proc voice_alert uv run python -m holdshort.voice_alert
wait_log gate "gate starting" 30
for a in safety procedures weather voice_alert; do wait_log "$a" "READY $a" 30; done

echo "== starting API on :$API_PORT"
start_proc api uv run uvicorn holdshort.api.app:app --host 127.0.0.1 --port "$API_PORT"
wait_http "http://127.0.0.1:$API_PORT/health" '"change_feed":true' 40 api

echo "== starting web on :$WEB_PORT"
VITE_API_BASE="http://localhost:$API_PORT" VITE_WS_URL="ws://localhost:$API_PORT/ws" \
  start_proc web bash -c "cd web && exec npx vite --port $WEB_PORT --strictPort"
wait_http "http://127.0.0.1:$WEB_PORT/" "<div" 40 web

cat <<MSG
== Hold Short demo is up (database $DB_NAME)
   web  http://localhost:$WEB_PORT/
   api  http://localhost:$API_PORT/health
   logs $LOG_DIR/   stop: scripts/demo_stop.sh
   from a laptop: ssh -N -L $WEB_PORT:localhost:$WEB_PORT -L $API_PORT:localhost:$API_PORT hack-vm
MSG
