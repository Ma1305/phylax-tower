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
