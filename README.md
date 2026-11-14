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

## Audit agent

`holdshort/audit/` is read-only (aggregate/distinct only). `timeline(entity, around=None)`
runs one aggregation over transmissions, `intents_*`, decisions, permissions and
security_events and returns items `{t, kind, actor, summary, rule, authorized_by,
approved_by, doc_id}` in scenario order. `explain(question)` uses the LLM (`LLM_API_KEY`,
`LLM_MODEL`) to pick the entity/time and write a narrative citing `[doc_id]`s; a narrative
that cites anything outside the timeline is discarded for a deterministic one.

```bash
uv run python -m holdshort.audit "Why was TRUCK1 held?"
```
