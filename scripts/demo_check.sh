#!/usr/bin/env bash
# End-to-end demo check against Atlas, in a throwaway database.
#
#   scripts/demo_check.sh
#
# Starts the gate + safety/procedures/weather agents, plays the LaGuardia scenario
# twice in text mode at --speed 5 (safety OFF, then ON) and asserts:
#   1. run 1 has no executed stop decision (a stop the gate rejected as
#      agent_disabled is allowed: it is the audit trail of "safety was off")
#   2. run 2 has an executed stop decision notifying TRUCK1
#   3. the procedures proposal is pending_approval
#   4. rogue() produces 2 security_events (both writes blocked by Atlas)
# Every process it starts is stopped on exit. Logs: logs/demo_check/.
#
# Database: $DEMO_CHECK_DB (default "<MONGODB_DB>_demo_check"). It is dropped and
# set up again at the start, so the check never touches the live demo database.
set -euo pipefail

cd "$(dirname "$0")/.."
LOG_DIR=logs/demo_check
mkdir -p "$LOG_DIR"
rm -f "$LOG_DIR"/*.log

BASE_DB=$(uv run python -c 'from holdshort.env import mongodb_db_name; print(mongodb_db_name())')
export MONGODB_DB="${DEMO_CHECK_DB:-${BASE_DB}_demo_check}"
case "$MONGODB_DB" in
  *_demo_check|*_test) ;;
  *) echo "refusing to drop database '$MONGODB_DB' (name must end in _demo_check)"; exit 2 ;;
esac

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
uv run python - <<'EOF'
from holdshort.db import get_client
from holdshort.env import mongodb_db_name
c = get_client(); c.drop_database(mongodb_db_name()); c.close()
EOF
uv run python scripts/setup_atlas.py > "$LOG_DIR/setup.log"

start_proc() {  # name module...
  local name=$1; shift
  uv run python -m "$@" > "$LOG_DIR/$name.log" 2>&1 &
  PIDS+=("$!")
}
wait_log() {  # file pattern timeout_s
  local i
  for ((i = 0; i < $3 * 10; i++)); do
    grep -q "$2" "$1" 2>/dev/null && return 0
    sleep 0.1
  done
  echo "FAIL: '$2' not seen in $1 within $3 s"; cat "$1"; exit 1
}

echo "== starting gate + agents"
start_proc gate holdshort.gate
start_proc safety holdshort.agents.safety_agent
start_proc procedures holdshort.agents.procedures_agent
start_proc weather holdshort.agents.weather_agent
wait_log "$LOG_DIR/gate.log" "gate starting" 30
for a in safety procedures weather; do wait_log "$LOG_DIR/$a.log" "READY $a" 30; done
sleep 2  # let the gate's change stream open

run() {  # safety on|off -> prints the run marker
  uv run python -m holdshort.scenario.runner start --reset --safety "$1" --speed 5 --mode text \
    2> "$LOG_DIR/run_$1.log" | tail -1
}

echo "== run 1: safety OFF"
RUN1=$(run off)
echo "$RUN1"
sleep 4  # let the gate decide anything still in flight
echo "== run 2: safety ON"
RUN2=$(run on)
echo "$RUN2"

echo "== rogue agent"
uv run python -m holdshort.agents.procedures_agent rogue > "$LOG_DIR/rogue.log" 2>&1
cat "$LOG_DIR/rogue.log" | grep -v ' WARNING ' || true

echo "== assertions"
RUN1="$RUN1" RUN2="$RUN2" uv run python - <<'EOF'
import json, os, sys, time
from bson import ObjectId
from holdshort.db import get_db

db = get_db()
r1, r2 = json.loads(os.environ["RUN1"]), json.loads(os.environ["RUN2"])
m1, m2 = ObjectId(r1["marker"]), ObjectId(r2["marker"])
results = []

def check(name, ok, detail=""):
    results.append(ok)
    print(f"{'PASS' if ok else 'FAIL'}  {name}{'  -- ' + detail if detail else ''}")

def wait_for(query, timeout=20):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        d = db.decisions.find_one(query)
        if d:
            return d
        time.sleep(0.25)
    return None

run1 = {"_id": {"$gte": m1, "$lt": m2}}
stops1 = list(db.decisions.find({**run1, "intent_action": "stop"}))
check("run 1 (safety off): no executed stop decision",
      not any(d["outcome"] == "executed" for d in stops1),
      f"stop decisions in run 1: {[(d['outcome'], d['rule']) for d in stops1]}")

stop2 = wait_for({"_id": {"$gte": m2}, "intent_action": "stop", "outcome": "executed",
                  "notified": "TRUCK1"})
check("run 2 (safety on): executed stop notifying TRUCK1", stop2 is not None,
      f"rule={stop2['rule']} notified={stop2['notified']} authorized_by={stop2['authorized_by']}"
      if stop2 else "none found")
if stop2:
    intent = db.intents_safety.find_one({"_id": stop2["intent_id"]})
    print(f"      intent: {intent['reason']}, conflicts_with {intent['conflicts_with']!r}")

prop = wait_for({"_id": {"$gte": m2}, "agent": "procedures",
                 "intent_action": "propose_instruction"})
pi = db.intents_procedures.find_one({"_id": prop["intent_id"]}) if prop else None
check("procedures proposal is pending_approval",
      bool(prop) and prop["outcome"] == "pending_approval",
      f"{prop['outcome']}: {pi['proposal_text']!r}" if prop else "no proposal decision")
check("proposal text", bool(pi) and pi["proposal_text"]
      == "TRUCK1 cross runway 04 at D after AC8646 passes",
      repr(pi["proposal_text"]) if pi else "")

ev = list(db.security_events.find())
check("rogue(): 2 security_events, both blocked by Atlas",
      len(ev) == 2 and all(e["error_code"] == 121 for e in ev),
      "; ".join(f"code={e['error_code']} {e['attempted']}" for e in ev))
check("rogue(): nothing rogue reached intents/decisions",
      db.intents_procedures.count_documents({"action": "clear"}) == 0
      and db.decisions.count_documents({"rule": "rogue_self_approved"}) == 0)

perm = db.permissions.find_one({"agent": "safety"})
check("safety permission changed by controller_A", perm["by"] == "controller_A")
check("run 2: TRUCK1 held at D", r2["truck_held"] and not r1["truck_held"],
      f"run1 held={r1['truck_held']} run2 held={r2['truck_held']}")
sys.exit(0 if all(results) else 1)
EOF
echo "== demo_check PASSED"
