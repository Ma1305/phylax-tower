#!/usr/bin/env bash
# Runs the section-6 checklist. Exit code 2 from a check means BLOCKED (missing secret).
cd "$(dirname "$0")/../.."
run() { echo "--- $1"; shift; "$@"; }
run "1 toolchain" scripts/verify/check_toolchain.sh
run "2+3 MCP server" uv run python scripts/verify/mcp_smoke.py
run "4 change stream" uv run python scripts/verify/change_stream.py
run "5 \$jsonSchema validator" uv run python scripts/verify/schema_validator.py
run "6 LLM call" uv run python scripts/verify/llm_call.py
run "7 ElevenLabs" uv run python scripts/verify/elevenlabs_tts.py
run "8 git hygiene" scripts/verify/check_git.sh
