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

## Checkpoints (LangGraph + MongoDB)

The safety and procedures agent loops are LangGraph graphs (safety: evaluate -> emit ->
persist; procedures: check -> propose -> done) checkpointed to Atlas with
`MongoDBSaver` (`checkpoints_<agent>` / `checkpoint_writes_<agent>` in the same database).
The `thread_id` is the scenario run id: `scenario.runner start` inserts a
`scenario_runs` document whose `_id` is the run marker. A killed agent loads its
thread's checkpoint on restart, finishes an interrupted step if it died between nodes,
catches up only on documents it missed, and skips anything already checkpointed. The
safety decision is still the deterministic rule engine (no LLM). LangSmith tracing is
on only if `LANGSMITH_API_KEY` is set.

    scripts/crash_demo.sh   # kill -9 the safety agent mid-run, restart, assert resume + stop

It uses a throwaway `<MONGODB_DB>_crash_demo` database (`$CRASH_DEMO_DB` to override);
logs go to `logs/crash_demo/`.
