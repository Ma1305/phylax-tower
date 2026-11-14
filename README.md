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
