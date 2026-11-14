#!/usr/bin/env bash
# Launches the local MongoDB MCP server (Path 1, read-write) for Claude Code.
# The connection string is read from the gitignored .env at the repo root, so
# the MCP config itself never contains a secret.
set -euo pipefail
ROOT="${HOLDSHORT_ROOT:-$(cd "$(dirname "$0")/.." && pwd)}"
ENV_FILE="${HOLDSHORT_ENV:-$ROOT/.env}"
# Worktrees of the repo fall back to the shared secrets file.
if [[ -z "${HOLDSHORT_ENV:-}" && ! -f "$ENV_FILE" && -f "$HOME/plane-management/.env" ]]; then
  ENV_FILE="$HOME/plane-management/.env"
fi
if [[ -f "$ENV_FILE" ]]; then set -a; . "$ENV_FILE"; set +a; fi
if [[ -n "${MONGODB_URI:-}" ]]; then export MDB_MCP_CONNECTION_STRING="$MONGODB_URI"; fi
exec npx -y mongodb-mcp-server@latest "$@"
