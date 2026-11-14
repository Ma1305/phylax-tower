#!/usr/bin/env bash
# Headless end-to-end check of the live demo stack, driven through the API like the UI.
#
#   scripts/demo_api_check.sh
#
# Drops + sets up a throwaway database "<MONGODB_DB>_demo_api" ($DEMO_API_DB), starts
# scripts/demo.sh against it (API_PORT/WEB_PORT default 8000/5173; scenario speed x5),
# runs scripts/demo_api_check.py, and always stops the stack with scripts/demo_stop.sh.
set -euo pipefail
cd "$(dirname "$0")/.."

BASE_DB=$(uv run python -c 'from holdshort.env import mongodb_db_name; print(mongodb_db_name())')
export MONGODB_DB="${DEMO_API_DB:-${BASE_DB}_demo_api}"
case "$MONGODB_DB" in
  *_demo_api|*_test) ;;
  *) echo "refusing to drop database '$MONGODB_DB' (name must end in _demo_api)"; exit 2 ;;
esac
export API_PORT=${API_PORT:-8000} WEB_PORT=${WEB_PORT:-5173}
export HOLDSHORT_SCENARIO_SPEED=${HOLDSHORT_SCENARIO_SPEED:-5}

trap 'scripts/demo_stop.sh' EXIT
echo "== database $MONGODB_DB: drop (demo.sh sets it up again)"
uv run python - <<'PY'
from holdshort.db import get_client
from holdshort.env import mongodb_db_name
c = get_client(); c.drop_database(mongodb_db_name()); c.close()
PY
scripts/demo.sh
API="http://127.0.0.1:$API_PORT" uv run python scripts/demo_api_check.py 2>&1 | tee logs/demo/api_check.log
exit "${PIPESTATUS[0]}"
