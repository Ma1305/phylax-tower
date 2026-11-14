#!/usr/bin/env bash
# Check 1: node >= 22.12.0, python >= 3.11
set -u
ok=0
node_v=$(node --version 2>/dev/null | sed 's/^v//') || node_v=""
if [[ -z "$node_v" ]]; then echo "FAIL: node not found"; ok=1
elif [[ "$(printf '%s\n22.12.0\n' "$node_v" | sort -V | head -1)" == "22.12.0" ]]; then echo "PASS: node v$node_v"
else echo "FAIL: node v$node_v < 22.12.0"; ok=1; fi
py_v=$(python3 -c 'import sys;print("%d.%d.%d"%sys.version_info[:3])' 2>/dev/null) || py_v=""
if python3 -c 'import sys;sys.exit(0 if sys.version_info>=(3,11) else 1)' 2>/dev/null; then echo "PASS: python $py_v"
else echo "FAIL: python3 ${py_v:-missing} < 3.11"; ok=1; fi
exit $ok
