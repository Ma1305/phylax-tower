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
  and return 501 until those exist (`HOOKS` in `holdshort/api/app.py`).
- `GET /health`. The gate runs as its own process; `HOLDSHORT_API_RUN_GATE=1` runs it
  inside the API instead.
