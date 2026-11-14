# scripts: set up, run and check the stack

Run them from the repo root. They read `.env` there (see [`.env.example`](../.env.example)).

| Script | What it does |
|---|---|
| `setup_atlas.py` | Creates or updates the collections, `$jsonSchema` validators and indexes, and seeds the airport, permissions and rule sets. Idempotent. `--db NAME` |
| `setup_search.py` | Seeds `callsigns` and `incidents` and builds the Atlas Search and Vector Search indexes. Slow, so `run_v2.sh` runs it in the background |
| `run_v2.sh`, `stop_v2.sh` | Start and stop the tower: database setup, then every process (`python -m holdshort.serve`), and wait for `/health` |
| `run_all.sh`, `stop_all.sh` | Start and stop everything: the tower (8000), the visualizer API (8100) and the web app (5180). Logs and pid files in `logs/v2/` |
| `feed.sh` | Feeds the tower a small scripted scenario (`normal`, `incident`, `intruder`, `both`) through `tools/smoke_feed.py` |
| `e2e_check.sh` | End-to-end check on a throwaway database: the incident with stops autonomous, needing approval, and warn-only, plus a normal run; then audits the decision log. Driven by `e2e_check.py` |
| `viz_integration.sh` | Playwright run of the web app against the real tower. Written before the final redesign of the screen and not updated for it |
| `voice_proof.py` | Exercises the real ElevenLabs speech path once (text-to-speech, voice isolation, speech-to-text) on a throwaway database |
| `llm_proof.py` | Three real calls to the configured language model: parse a call, draft a proposal, answer an audit question. No database needed |
| `mcp-mongodb.sh` | Starts the MongoDB MCP server with the connection string from `.env`, for inspecting the data from a coding agent |
| `audio/` | Text-to-speech helper and `.env` loader imported by `tools/smoke_feed.py` |

The usual sequence is in [`TESTING.md`](../TESTING.md): `run_all.sh`, a scenario from the
simulator or `feed.sh`, then `stop_all.sh`. The check scripts need an Atlas connection string;
`e2e_check.sh` and `viz_integration.sh` also need Linux tools (`ss`, `shuf`).
