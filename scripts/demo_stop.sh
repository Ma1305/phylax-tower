#!/usr/bin/env bash
# Stop everything scripts/demo.sh started (each line of logs/demo/pids is a process group).
set -uo pipefail
cd "$(dirname "$0")/.."
PID_FILE=logs/demo/pids
if [ ! -s "$PID_FILE" ]; then
  echo "nothing to stop ($PID_FILE missing or empty)"; rm -f "$PID_FILE"; exit 0
fi

while read -r pid name; do
  [ -n "$pid" ] && kill -TERM -- "-$pid" 2>/dev/null && echo "stopping $name ($pid)"
done < "$PID_FILE"
for _ in $(seq 50); do
  alive=0
  while read -r pid _n; do kill -0 -- "-$pid" 2>/dev/null && alive=1; done < "$PID_FILE"
  [ "$alive" = 0 ] && break
  sleep 0.2
done
while read -r pid name; do
  if kill -0 -- "-$pid" 2>/dev/null; then
    kill -KILL -- "-$pid" 2>/dev/null; echo "killed $name ($pid)"
  fi
done < "$PID_FILE"
rm -f "$PID_FILE"
echo "demo stopped"
