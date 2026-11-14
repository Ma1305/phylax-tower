#!/usr/bin/env bash
# Feed the tower with the smoke feed (tools/smoke_feed.py, /ingest/* only):
#   scripts/feed.sh normal|incident|intruder|both [--loop] [extra smoke_feed options]
# Tower address: TOWER_API (default http://localhost:8000). Extra options are passed on,
# e.g. --speed 2, --no-audio, --no-reset. With --loop it repeats until Ctrl-C; its pid is in
# logs/v2/feed.pid so scripts/stop_all.sh stops a feed left running in the background.
set -euo pipefail
cd "$(dirname "$0")/.."
case "${1:-}" in
  normal|incident|intruder|both) SCENARIO="$1"; shift ;;
  *) echo "usage: $0 normal|incident|intruder|both [--loop] [--speed N] [--no-audio] [--no-reset]" >&2; exit 2 ;;
esac
LOGS="${HOLDSHORT_LOGS:-logs/v2}"
mkdir -p "$LOGS"
echo $$ > "$LOGS/feed.pid"
exec uv run python tools/smoke_feed.py --api "${TOWER_API:-http://localhost:8000}" --scenario "$SCENARIO" "$@"
