# Hold Short

AI agents that listen to simulated tower radio, store every call in MongoDB Atlas,
and stop a runway conflict before it happens. The AI can stop traffic, never clear it.

## Setup

Requirements: Python 3.12 with [uv](https://docs.astral.sh/uv/), Node 22.12+ (for `web/`),
and an Atlas cluster you can reach.

1. Copy `.env.example` to `.env` (gitignored, never commit it) and fill in `MONGODB_URI`.
   `MONGODB_DB` defaults to `holdshort`. On the shared VM the file lives at
   `~/plane-management/.env`; `holdshort/env.py` finds it from any worktree
   (lookup order: `$HOLDSHORT_ENV`, `<repo>/.env`, `~/plane-management/.env`).
2. Install dependencies (the `holdshort` package is installed in editable mode):

   ```bash
   uv sync
   ```

3. Create the collections, `$jsonSchema` validators, indexes and seed data
   (idempotent, safe to re-run; it never resets live permission switches or the
   active rule set):

   ```bash
   uv run python scripts/setup_atlas.py
   ```

4. Check everything:

   ```bash
   uv run ruff check .
   uv run pytest          # validator tests use the throwaway "<MONGODB_DB>_test" database
   ```

`holdshort/contracts.py` is the single source of truth for documents; the validators
in `scripts/setup_atlas.py` read their enums from it. `SETUP_STATUS.md` covers the
VM setup checks in `scripts/verify/`.

## API

```bash
uv run uvicorn holdshort.api.app:app --port 8000   # CORS open for localhost origins
```

- `WS /ws`: first a `{"type": "snapshot", "doc": {permissions, rules, transmissions,
  decisions, flight_state, runway_status}}`, then one `{"type", "doc", "collection", "op"}`
  per change (`type`: transmission | decision | permission | state). ObjectIds are strings,
  datetimes ISO 8601 with `Z`.
- `POST /permissions/{agent}` `{enabled, by}`, `POST /approve` `{intent_id, controller}`,
  `POST /revoke` `{decision_id, controller}` go through `holdshort.gate`.
- `POST /scenario/start` `{safety_enabled, mode: "text"|"audio"}`, `POST /scenario/reset`,
  `POST /rogue`, `GET /audit?q=` call `holdshort.scenario.start(db, *, safety_enabled, mode)`,
  `holdshort.scenario.reset(db)`, `holdshort.agents.rogue(db)`, `holdshort.audit.answer(db, q)`
  (`HOOKS` in `holdshort/api/app.py`). All exist except `holdshort.audit` (audit task),
  so `/audit` still returns 501. `/scenario/start` returns at once and plays in a thread of
  the API (`HOLDSHORT_SCENARIO_SPEED`, default 1); `GET /scenario/status` reports the run.
- Decision events and snapshot decisions carry a read-only `intent` view
  (`action, target, runway, reason, conflicts_with, proposal_text`) for the UI.

## Live demo

`scripts/demo.sh` starts gate, agents, API (:8000) and web (:5173), logs in `logs/demo/`;
`scripts/demo_stop.sh` stops it all. Runbook with the exact clicks: [DEMO.md](DEMO.md).
`scripts/demo_api_check.sh` checks the whole stack headlessly through the API.
- `GET /health`. The gate runs as its own process; `HOLDSHORT_API_RUN_GATE=1` runs it
  inside the API instead.

## Listener agent

```bash
uv run python -m holdshort.listener scenarios/laguardia/audio/medium/          # insert into MONGODB_DB
uv run python -m holdshort.listener scenarios/laguardia/audio/medium/ --check  # + compare with script `expect`
uv run python -m holdshort.listener --text --dry-run                           # script lines, no insert
```

- Audio mode (VOICE_ENABLED=true + ELEVENLABS_API_KEY): `clean()` (ElevenLabs voice isolation,
  passthrough on failure) -> `transcribe()` (ElevenLabs STT, confidence = mean word probability)
  -> `parse()` -> readback check -> `insert()` (insert-only).
- Text mode (`--text`, voice disabled, or no clips on disk): lines from `scenarios/laguardia/script.json`,
  simulated confidence 0.95 (0.55 for lines marked `"unclear": true`).
- `parse()` labels with the Anthropic LLM (`LLM_API_KEY`, `LLM_MODEL`, default `claude-opus-5-5`,
  structured JSON output). Without a key it falls back to an offline phraseology labeller
  (`holdshort/listener/rules.py`); force one with `--labeller llm|rules`.
  Values are always normalised in code: "Truck one and company" -> TRUCK1, "the jet" -> AC8646, "4" -> "04", "Delta" -> "D".
- Confidence < 0.8 or an unparseable call -> kind `other`, `verified: false`, transcript kept,
  `flag: "unclear - say again"`. A "stop" with no callsign -> kind `stop`, `to: "UNKNOWN"`, flagged.
- `readback`: clearance -> `missing` (awaiting readback), readback -> `matched`/`mismatch` vs the last
  clearance to that callsign, everything else -> `not_applicable`.

## Agents and scenario runner

Each agent is its own process. Agents never act: they only insert intents into
`intents_<agent>`; the gate (`python -m holdshort.gate`) decides.

    uv run python -m holdshort.agents.safety_agent       # transmissions -> stop intents
    uv run python -m holdshort.agents.procedures_agent   # executed crossing stop -> proposal
    uv run python -m holdshort.agents.weather_agent      # METAR < 1 sm -> set_rule_set low_vis_v1
    uv run python -m holdshort.agents.procedures_agent rogue   # blocked writes -> security_events

    uv run python -m holdshort.scenario.runner reset     # clears flight_state + runway_status only
    uv run python -m holdshort.scenario.runner start --safety on --speed 5 --mode text

`scripts/demo_check.sh` runs the whole thing (gate + agents, scenario with safety off
then on, rogue agent) in a throwaway `<MONGODB_DB>_demo_check` database and asserts
the demo outcomes. Logs go to `logs/demo_check/`.

## Search (tier 2)

```bash
uv run python scripts/setup_search.py   # idempotent: collections, seeds, indexes, waits for READY
```

- `holdshort.search.callsigns.resolve("truck one")` -> `Match(callsign="TRUCK1", score=...)`
  (spoken words normalized to callsign form, then fuzzy `$search` on `callsigns_search`).
- `holdshort.search.callsigns.similar_on_frequency("AC8646")` -> confusable callsigns on the
  same frequency (AC6846, AC8464) with a reason.
- `holdshort.search.incidents.similar(text, k=3)` -> past runway incursions. Uses `$vectorSearch`
  with automated embedding (`voyage-4` on `summary`, index `incidents_vector`), and falls back to
  the Atlas Search text index `incidents_text` if the vector index is not queryable.
  The incident summaries are written in our own words and name no airlines or people.

## Audit agent

`holdshort/audit/` is read-only (aggregate/distinct only). `timeline(entity, around=None)`
runs one aggregation over transmissions, `intents_*`, decisions, permissions and
security_events and returns items `{t, kind, actor, summary, rule, authorized_by,
approved_by, doc_id}` in scenario order. `explain(question)` uses the LLM (`LLM_API_KEY`,
`LLM_MODEL`) to pick the entity/time and write a narrative citing `[doc_id]`s; a narrative
that cites anything outside the timeline is discarded for a deterministic one.
`answer(db, q)` is the hook `GET /audit?q=...` in `holdshort/api/app.py` calls.

```bash
uv run python -m holdshort.audit "Why was TRUCK1 held?"
```
