#!/usr/bin/env bash
# Check 8: no .env / MCP config tracked or staged; GitHub repo is public.
cd "$(dirname "$0")/../.."
bad=$(git ls-files --cached | grep -E '(^|/)\.env($|\.)|(^|/)\.?mcp[^/]*\.json$' | grep -v '^\.env\.example$')
if [[ -n "$bad" ]]; then echo "FAIL 8: tracked/staged secret-bearing files: $bad"; exit 1; fi
for f in .env .mcp.json; do
  git check-ignore -q "$f" || { echo "FAIL 8: $f is not gitignored"; exit 1; }
done
vis=$(gh repo view Ma1305/plane-management --json visibility -q .visibility 2>&1)
[[ "$vis" == "PUBLIC" ]] || { echo "FAIL 8: repo visibility: $vis"; exit 1; }
echo "PASS 8: no .env or MCP config tracked/staged; .env and .mcp.json ignored; repo is PUBLIC"
