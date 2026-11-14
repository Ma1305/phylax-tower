#!/usr/bin/env bash
# The visualizer against the real tower, end to end, on a throwaway database:
#   scripts/viz_integration.sh
# Starts everything with scripts/run_all.sh on free ports in 18000-18999, drives the web app
# with Playwright (visualizer/web/scripts/integration.mjs: incident feed, autonomous stop,
# approvals, needs_approval, decision log, audit question, time travel), writes screenshots to
# docs/v2/screens/, then stops everything and drops the database. Voice is off (tone audio).
set -uo pipefail
cd "$(dirname "$0")/.."
TASK=$(git rev-parse --abbrev-ref HEAD 2>/dev/null | sed -n 's#^hq/\([0-9]*\)-.*#\1#p')
DB="holdshort_v2_t${TASK:-$$}_viz"
free() { for p in $(shuf -i 18000-18999 -n 200); do ss -ltn | grep -q ":$p " || { echo "$p"; return; }; done; }
TOWER_PORT=$(free); VIZ_PORT=$(free); WEB_PORT=$(free)
export HOLDSHORT_LOGS="logs/v2/viz_integration" VOICE_ENABLED=false
py() { uv run python - "$DB" "$@"; }
drop_db() {
  py <<'PY'
import sys
from holdshort.db import get_client
name = sys.argv[1]
assert name not in {"holdshort", "holdshort_v2"} and name.endswith("_viz"), name
c = get_client(); c.drop_database(name); c.close()
print(f"viz_integration: dropped database {name}")
PY
}
cleanup() { scripts/stop_all.sh; drop_db; }
trap cleanup EXIT
echo "viz_integration: database $DB, ports tower $TOWER_PORT viz $VIZ_PORT web $WEB_PORT"
drop_db
scripts/run_all.sh --db "$DB" --tower-port "$TOWER_PORT" --viz-port "$VIZ_PORT" --web-port "$WEB_PORT" \
  || { echo "FAIL  stack did not start"; exit 1; }
# Test fixture: one identity of each kind in agent_state, as holdshort/access.py connect_for
# records them (kind, username, role, expires_at; never a password), so the agent switches
# have all three kinds to show whatever credentials this machine has.
py <<'PY'
import sys
from datetime import UTC, datetime, timedelta
from holdshort.db import get_client
c = get_client(); db = c[sys.argv[1]]
now = datetime.now(UTC)
for proc, ident in {
    "safety_agent": {"kind": "generated", "username": "hs-safety-t40viz", "role": "hs_safety_role",
                     "expires_at": now + timedelta(hours=6)},
    "procedures_agent": {"kind": "static", "username": None, "role": None, "expires_at": None},
    "listener": {"kind": "shared", "username": None, "role": None, "expires_at": None},
}.items():
    db.agent_state.update_one({"_id": proc}, {"$set": {"identity": ident}}, upsert=True)
c.close()
PY
[[ -d visualizer/web/node_modules/playwright ]] || (cd visualizer/web && npm install --no-audit --no-fund >/dev/null)
(cd visualizer/web && node scripts/integration.mjs "http://localhost:$WEB_PORT" "http://localhost:$VIZ_PORT" "http://localhost:$TOWER_PORT")
rc=$?
[[ $rc -eq 0 ]] && echo "viz_integration: PASS" || echo "viz_integration: FAIL (logs in $HOLDSHORT_LOGS)"
exit $rc
