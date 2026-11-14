# Hold Short: setup status (2026-11-14)

Status of the section 6 checklist in the setup doc on the shared VM. Run every check again with:

```bash
uv sync                      # installs pymongo, python-dotenv, anthropic, requests into .venv
scripts/verify/run_all.sh    # any check that exits with code 2 is BLOCKED (a secret is missing)
```

The scripts read `.env` from the repo root. Set `HOLDSHORT_ENV=/path/to/.env` to use a different file.

| # | Check | Status | Script |
|---|-------|--------|--------|
| 1 | node >= 22.12, python >= 3.11 | PASS (node v26.13.1, python 3.12.3) | `scripts/verify/check_toolchain.sh` |
| 2 | MCP: list databases | BLOCKED: no `MONGODB_URI` | `scripts/verify/mcp_smoke.py` |
| 3 | MCP: insert/read/drop in `holdshort.smoke_test` | BLOCKED: no `MONGODB_URI` | `scripts/verify/mcp_smoke.py` |
| 4 | pymongo change stream | BLOCKED: no `MONGODB_URI` | `scripts/verify/change_stream.py` |
| 5 | `$jsonSchema` validator rejects a bad insert | BLOCKED: no `MONGODB_URI` | `scripts/verify/schema_validator.py` |
| 6 | One LLM call | BLOCKED: no `LLM_API_KEY` | `scripts/verify/llm_call.py` |
| 7 | ElevenLabs TTS clip | ElevenLabs pending (no key yet) | `scripts/verify/elevenlabs_tts.py` |
| 8 | No `.env` or MCP config tracked; repo is public | PASS | `scripts/verify/check_git.sh` |

Checks 2 to 5 were run against a throwaway local MongoDB 8 replica set in rootless podman, and all four passed. This only shows the scripts work. It does not count as a pass for the Atlas Sandbox cluster.

## What humans must provide

Put these in `~/plane-management/.env`. The file is gitignored. Copy `.env.example` to start.

1. **`MONGODB_URI`**: the `mongodb+srv://...` string for the **Atlas Sandbox** cluster. Create the project and cluster only through the emailed hackathon link. Before the checks can connect, a person also has to:
   - create a database user with read and write access (Database Access);
   - add this VM's public IP, or `0.0.0.0/0` for today only (Network Access).
2. **`LLM_API_KEY`**: an Anthropic API key. `llm_call.py` uses `claude-opus-5-5` by default; set `LLM_MODEL` to use another model. If the team uses a different provider, the script needs a small change.
3. **`ELEVENLABS_API_KEY`** (optional): add it once the coupon is redeemed. Until then, `VOICE_ENABLED=false`.

After adding them, restart Claude Code so the MongoDB MCP server picks up the connection string, then run `scripts/verify/run_all.sh`.

## MongoDB MCP server (Path 1, local, read-write)

- `~/plane-management/.mcp.json` is gitignored and contains no secret. It sources `~/plane-management/.env`, exports `MONGODB_URI` as `MDB_MCP_CONNECTION_STRING`, and runs `npx -y mongodb-mcp-server@latest`. Read-only mode is not set, so the write tools (create-collection, create-index, insert-many, drop-collection) are available.
- `scripts/mcp-mongodb.sh` is a committed wrapper that does the same thing. `mcp_smoke.py` uses it.
- The server registers the connection string under the connection ID `preconfigured`.
- Do not use the server's `atlas-local-*` tools or any Atlas admin tools to create clusters. The cluster must come from the sandbox link.

## MongoDB Agent Skills

Installed at project level with `npx skills add mongodb/agent-skills -a claude-code -y --skill '*'` into `.claude/skills/`, in this worktree and in `~/plane-management`. The skills include mongodb-mcp-setup, mongodb-connection, mongodb-schema-design and mongodb-natural-language-querying, plus a few others. They are gitignored. To install them on another machine, run the same command.
