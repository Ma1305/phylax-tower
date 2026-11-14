#!/usr/bin/env bash
# End-to-end check of the tower on a throwaway database and a free port in 18000-18999.
#   scripts/e2e_check.sh
# Starts the stack (scripts/run_v2.sh), runs scripts/e2e_check.py (smoke feed incident +
# normal, then incident with safety reactive; PASS / FAIL per assertion), then stops
# everything and drops the database. Exit code 0 only if every assertion passed.
set -uo pipefail
cd "$(dirname "$0")/.."
TASK=$(git rev-parse --abbrev-ref HEAD 2>/dev/null | sed -n 's#^hq/\([0-9]*\)-.*#\1#p')
DB="holdshort_v2_t${TASK:-$$}_e2e"
PORT=""
for p in $(shuf -i 18000-18999 -n 200); do
  ss -ltn | grep -q ":$p " || { PORT=$p; break; }
done
[[ -n "$PORT" ]] || { echo "e2e: no free port in 18000-18999" >&2; exit 2; }
export HOLDSHORT_LOGS="logs/v2/e2e"
drop_db() {
  uv run python - "$DB" <<'PY'
import sys
from holdshort.db import get_client
name = sys.argv[1]
assert name not in {"holdshort", "holdshort_v2"} and name.endswith("_e2e"), name
c = get_client(); c.drop_database(name); c.close()
print(f"e2e: dropped database {name}")
PY
}
cleanup() { scripts/stop_v2.sh; drop_db; }
trap cleanup EXIT
echo "e2e: database $DB, port $PORT, logs $HOLDSHORT_LOGS"
drop_db
scripts/run_v2.sh --db "$DB" --port "$PORT" || { echo "FAIL  stack did not start"; exit 1; }
uv run python scripts/e2e_check.py --api "http://127.0.0.1:$PORT" --db "$DB"
rc=$?
[[ $rc -eq 0 ]] && echo "e2e: PASS" || echo "e2e: FAIL (see $HOLDSHORT_LOGS/serve.log)"
exit $rc
