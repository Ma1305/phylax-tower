#!/usr/bin/env bash
# Crash demo: the safety agent is killed mid-run and resumes from its Atlas checkpoint.
#
#   scripts/crash_demo.sh
#
# Starts the gate + safety + procedures agents, plays the LaGuardia scenario with safety
# ON (text mode, --speed 2), and once the safety agent has checkpointed its 3rd
# transmission (the jet's landing clearance is in) kills it with SIGKILL. A few calls go
# by while it is down; then it is restarted. It must:
#   1. log RESUME with the checkpoint step it resumed from (thread_id = scenario run id)
#   2. catch up only on the calls it missed (each transmission processed exactly once,
#      across both processes: no history replay)
#   3. still fire the stop: an executed stop decision notifying TRUCK1, exactly one stop
#      intent for the run, TRUCK1 held at D, the procedures proposal pending_approval
# Every process it starts is stopped on exit. Logs: logs/crash_demo/.
#
# Database: $CRASH_DEMO_DB (default "<MONGODB_DB>_crash_demo"), dropped and set up
# again at the start.
set -euo pipefail

cd "$(dirname "$0")/.."
LOG_DIR=logs/crash_demo
mkdir -p "$LOG_DIR"
rm -f "$LOG_DIR"/*.log "$LOG_DIR"/*.json

uv sync -q
# run python directly (not through `uv run`) so SIGKILL hits the agent process itself
PY=$(uv run python -c 'import sys; print(sys.executable)')
BASE_DB=$("$PY" -c 'from holdshort.env import mongodb_db_name; print(mongodb_db_name())')
export MONGODB_DB="${CRASH_DEMO_DB:-${BASE_DB}_crash_demo}"
case "$MONGODB_DB" in
  *_crash_demo|*_demo_check|*_test) ;;
  *) echo "refusing to drop database '$MONGODB_DB' (name must end in _crash_demo)"; exit 2 ;;
esac
KILL_AFTER=${KILL_AFTER:-3}   # transmissions checkpointed before the crash
DOWN_S=${DOWN_S:-5}           # how long the safety agent stays dead

PIDS=()
cleanup() {
  for pid in "${PIDS[@]:-}"; do
    [ -n "$pid" ] && kill "$pid" 2>/dev/null || true
  done
  for pid in "${PIDS[@]:-}"; do
    [ -n "$pid" ] && wait "$pid" 2>/dev/null || true
  done
  echo "stopped ${#PIDS[@]} background processes"
}
trap cleanup EXIT

echo "== database $MONGODB_DB: drop + setup"
"$PY" - <<'EOF'
from holdshort.db import get_client
from holdshort.env import mongodb_db_name
c = get_client(); c.drop_database(mongodb_db_name()); c.close()
EOF
"$PY" scripts/setup_atlas.py > "$LOG_DIR/setup.log"

LAST_PID=
start_proc() {  # logname module...
  local name=$1; shift
  "$PY" -m "$@" > "$LOG_DIR/$name.log" 2>&1 &
  LAST_PID=$!
  PIDS+=("$LAST_PID")
}
wait_log() {  # file pattern timeout_s
  local i
  for ((i = 0; i < $3 * 10; i++)); do
    grep -qE "$2" "$1" 2>/dev/null && return 0
    sleep 0.1
  done
  echo "FAIL: '$2' not seen in $1 within $3 s"; cat "$1"; exit 1
}

echo "== starting gate + safety + procedures"
start_proc gate holdshort.gate
start_proc safety_1 holdshort.agents.safety_agent
SAFETY_PID=$LAST_PID
start_proc procedures holdshort.agents.procedures_agent
wait_log "$LOG_DIR/gate.log" "gate starting" 30
wait_log "$LOG_DIR/safety_1.log" "READY safety" 30
wait_log "$LOG_DIR/procedures.log" "READY procedures" 30
sleep 2  # let the gate's change stream open

echo "== playing the scenario (safety ON, x2)"
"$PY" -m holdshort.scenario.runner start --reset --safety on --speed 2 --mode text \
  > "$LOG_DIR/run.json" 2> "$LOG_DIR/run.log" &
RUNNER_PID=$!
PIDS+=("$RUNNER_PID")

wait_log "$LOG_DIR/safety_1.log" "CHECKPOINT .* processed=$KILL_AFTER\$" 60
echo "-- safety agent before the crash:"
grep -E "CHECKPOINT" "$LOG_DIR/safety_1.log" | sed 's/^/   /'
kill -9 "$SAFETY_PID"
wait "$SAFETY_PID" 2>/dev/null || true
echo "== KILLED safety agent (SIGKILL, pid $SAFETY_PID); down for ${DOWN_S}s while calls continue"
sleep "$DOWN_S"
grep -E " (AC8646|TRUCK1|TOWER): " "$LOG_DIR/run.log" | sed 's/^/   radio: /' | tail -4

echo "== restarting safety agent"
start_proc safety_2 holdshort.agents.safety_agent
wait_log "$LOG_DIR/safety_2.log" "READY safety" 60
grep -E "RESUME|CHECKPOINT" "$LOG_DIR/safety_2.log" | sed 's/^/   /'

wait "$RUNNER_PID"
sleep 3  # let the gate + procedures finish deciding
echo "-- safety agent after the restart:"
grep -E "CHECKPOINT|intent " "$LOG_DIR/safety_2.log" | sed 's/^/   /'

echo "== assertions"
LOG_DIR="$LOG_DIR" "$PY" - <<'EOF'
import json, os, re, sys, time
from pathlib import Path
from bson import ObjectId
from holdshort.agents.safety_agent import SafetyAgent
from holdshort.db import get_db

db = get_db()
logs = Path(os.environ["LOG_DIR"])
summary = json.loads((logs / "run.json").read_text().strip().splitlines()[-1])
run_id = ObjectId(summary["marker"])
s1, s2 = (logs / "safety_1.log").read_text(), (logs / "safety_2.log").read_text()
results = []

def check(name, ok, detail=""):
    results.append(ok)
    print(f"{'PASS' if ok else 'FAIL'}  {name}{'  -- ' + detail if detail else ''}")

def wait_for(coll, query, timeout=20):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        d = db[coll].find_one(query)
        if d:
            return d
        time.sleep(0.25)
    return None

check("scenario run recorded (run id = thread_id)",
      db.scenario_runs.find_one({"_id": run_id}) is not None, str(run_id))

m = re.search(r"RESUME thread=(\w+) from checkpoint step=(\d+) \(processed=(\d+), last=(\w+)\); "
              r"catching up (\d+) missed", s2)
check("restarted agent resumed from its Atlas checkpoint",
      bool(m) and m.group(1) == str(run_id),
      f"thread={m.group(1)} step={m.group(2)} processed={m.group(3)} missed={m.group(5)}"
      if m else "no RESUME line in safety_2.log")

n1 = len(re.findall(r"CHECKPOINT thread=", s1))
n2 = len(re.findall(r"CHECKPOINT thread=", s2))
total = db.transmissions.count_documents({"_id": {"$gte": run_id}})
check("each transmission processed exactly once across the crash (no replay)",
      n1 + n2 == total and n1 >= 1 and n2 >= 1,
      f"before crash {n1} + after restart {n2} = {n1 + n2}, transmissions {total}")

agent = SafetyAgent(db)
snap = agent.snapshot(run_id)
check("final checkpoint covers the whole run",
      snap.values.get("processed") == total and not snap.next,
      f"checkpoint step={snap.metadata.get('step')} processed={snap.values.get('processed')}")

stop = wait_for("decisions", {"_id": {"$gte": run_id}, "intent_action": "stop",
                              "outcome": "executed", "notified": "TRUCK1"})
check("stop still fires: executed stop notifying TRUCK1", stop is not None,
      f"rule={stop['rule']} notified={stop['notified']}" if stop else "none found")
stops = db.intents_safety.count_documents({"_id": {"$gte": run_id}})
check("exactly one stop intent for the run", stops == 1, f"{stops} stop intents")
check("TRUCK1 held at D", summary["truck_held"], f"truck_held={summary['truck_held']}")

prop = wait_for("decisions", {"_id": {"$gte": run_id}, "agent": "procedures",
                              "intent_action": "propose_instruction"})
check("procedures proposal pending_approval",
      bool(prop) and prop["outcome"] == "pending_approval",
      prop["outcome"] if prop else "no proposal decision")
sys.exit(0 if all(results) else 1)
EOF
echo "== crash_demo PASSED"
