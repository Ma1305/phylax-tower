# holdshort: the tower

The product itself. The package kept its build-time name; the product is called Phylax Tower.

One API and a set of workers around one Atlas database. Every worker follows a change stream,
resumes from a token it stores in `agent_state`, and takes `--db NAME`.

| Process | Code | What it does |
|---|---|---|
| api | `api/` | FastAPI: `/ingest/*` (data in), `/control/*` (controller actions, decision log, audit question), `/health` |
| listener | `listener/` | Queued radio clips to transcripts to parsed transmissions, many streams at once. Rules first, the model only for calls the rules cannot parse |
| state | `state/` | Positions and transmissions to clearances and `lane_status` (clear, reserved, occupied, conflict, closed) |
| memory | `memory/` | The agents' working memory: one short, expiring `context` line per fact |
| safety agent | `safety/`, `agents/safety_agent.py` | Deterministic rules over the live picture. Emits `warn` and `stop` intents. No language model |
| procedures agent | `agents/procedures_agent.py` | The advisor, a LangGraph graph checkpointed in MongoDB. Proposes the next instruction for the controller to approve |
| weather agent | `agents/weather_agent.py` | Asks for the low-visibility rule set when visibility drops |
| gate | `gate/` | Turns intents into decisions: switch, mode, tier, lane owner. Also approve, reject and revoke |
| speaker | `speaker/` | Speaks executed stops and approved instructions; stores the audio and a synthetic transmission |

Other files worth opening:

- `contracts.py`: one Pydantic model per collection; the Atlas validators are built from its enums.
- `access.py`: the per-agent database handle. A read or write outside the agent's permission
  lists raises and is recorded in `security_events`.
- `identity/`: optional per-agent Atlas database users generated from the permission documents
  (see [`identity/README.md`](identity/README.md)).
- `audit/`: the audit agent. One aggregation over the insert-only logs, and a narrative that may
  only cite records in that timeline.
- `llm.py`: the only place that calls a language model (Gemini on Vertex AI by default).
- `search/`: fuzzy callsign matching (Atlas Search) and similar incidents (Vector Search).
- `serve.py`: runs every process in one container.

## Run

```bash
uv sync                                               # from the repo root
scripts/run_v2.sh                                     # database setup, then every process; logs in logs/v2/
uv run python -m holdshort.serve --only api,gate      # or a subset, in the foreground
uv run python -m holdshort.agents.safety_agent        # or one worker alone
```

Settings come from `.env` at the repo root (see [`.env.example`](../.env.example)). Tests:
`uv run pytest -q` from the repo root; they need `MONGODB_URI` and use a throwaway database.
