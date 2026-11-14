# Hold Short v2: build plan

Read this first, then `CONTRACTS.md` (data shapes and APIs). `SIMULATOR_CONTRACT.md` is the
only source for the `/ingest/*` API. `world_lga.json` is the seeded airport.

Hackathon: MongoDB "Agents in Context", statement #1 "Agents You Can Audit". Submission 5:00 PM
today. Everything lives in the MongoDB Atlas Sandbox cluster, database `holdshort_v2`.
The v1 build (branch `build/all-tasks`, database `holdshort`) is the fallback demo: never touch
that database.

## 1. Product

Three separate systems:

1. **Tower (main system, package `holdshort/`)**. One tower, several controllers, each owning
   lanes (runways, taxiways, aprons). It takes in many radio streams and position feeds at the
   same time, keeps a live picture (objects, lane status), keeps a small expiring working memory
   for the agents, and runs agents that warn (reactive) or propose actions that the owning
   controller approves (proactive).
2. **Visualizer (`visualizer/`)**. Separate system: its own read-only API and its own web app.
   It never imports from `holdshort/` and never writes to the database. Controller actions go
   to the tower's `/control/*` API.
3. **Simulator (`simulator/`, built by another session)**. Feeds the tower through `/ingest/*`
   only. Not part of this build; `tools/smoke_feed.py` is our small stand-in for tests.

Core rule, unchanged: **the AI can warn, stop or propose. It can never clear traffic. The
controller stays in charge.**

### What the judges asked for (statement #1), and where we answer it

The statement: use Atlas as the identity, policy and audit layer (collections, schema
validation, change streams) for an agent that takes real actions, where every action is tied to
an identity, checked against policy before it runs, and recorded so a reviewer can reconstruct
it afterward. Keep these three answers visible in the code, the data and the screen:

| Their question | Our answer |
|---|---|
| How do you decide what an agent may do and enforce it before the action? | Agents only insert intents. Atlas validators, the `ScopedDb` access lists and the gate (mode, tier, lane owner) all run before any decision executes. |
| How do you record what it did, why, and under whose authority? | Insert-only `decisions` with `authorized_by`, `controller_id`, `approved_by`; `security_events` for every blocked attempt; `state_events` for every switch change. |
| How do you stop or roll back an agent mid-run? | Per-agent and master switches, mode and tier changes take effect on the next intent; `revoke` inserts a new decision pointing at the old one. |

The visualizer's time travel is the "reviewer reconstructs it afterward" feature: build it
from the stored records only.

Submission needs a public repo, an accessible demo link and a 1-minute video. Banned: basic
RAG, Streamlit, and projects where a dashboard is the main feature, so the tower and its
agents are the product and the visualizer is a separate viewer.

Sponsor technology to use where it fits (do not bolt it on): Atlas schema validation, change
streams, time-series and TTL collections, Atlas Search / Vector Search with automated
(Voyage) embeddings for the agents' working memory and similar incidents, LangGraph with
MongoDB checkpoints (+ LangSmith tracing when `LANGSMITH_API_KEY` is set) for the LLM
advisor, ElevenLabs for speech-to-text, voice isolation and the spoken outbound calls.

## 2. Architecture

```
 simulator / real feeds
        |  HTTP  /ingest/*  (positions, audio clips per stream, weather)
        v
+-------------------------- TOWER (holdshort/) ---------------------------+
| api (FastAPI :8000)   /ingest/*  /control/*  /health                    |
| listener   audio_clips(queued) -> speech-to-text -> transmissions       |
| state      objects + transmissions -> lane_status, clearances           |
| memory     transmissions/decisions/weather -> context (expiring)        |
| agents     safety (rules), procedures (LLM advisor), weather            |
|            agents only INSERT intents_<agent>; they never act           |
| gate       intents_* -> decisions (mode, tier, lane owner routing)      |
| speaker    executed stop / approved instruction -> outbound audio clip  |
+-------------------------------------------------------------------------+
        |                         MongoDB Atlas  (holdshort_v2)
        |  world: towers, lanes, controllers, streams
        |  live:  objects (TTL), lane_status, context (TTL)
        |  log:   transmissions, audio (GridFS), intents_*, decisions,
        |         positions (time series), state_events, security_events
        v
+------------------------ VISUALIZER (visualizer/) -----------------------+
| api (FastAPI :8100, read-only)  /viz/*  live WebSocket, time travel     |
| web (React + Vite :5180)        map, streams bar, timeline, controllers |
+-------------------------------------------------------------------------+
```

## 3. Design decisions (do not re-open these)

1. **Time is wall-clock UTC everywhere.** `t` is when it happened in real time. No scenario
   clock. TTL and time travel both depend on this.
2. **A radio stream is a sequence of utterance clips.** Radio is push-to-talk, so each
   transmission arrives as one audio clip tagged with its `stream_id`. Many streams are
   processed at the same time (worker pool). No continuous PCM streaming in this build.
3. **Permanent log vs. working memory.** `transmissions`, `decisions`, `intents_*`,
   `state_events`, `security_events` are insert-only and never expire. Agents do NOT search
   them. Agents search `context`: small, one line per fact, superseded and expired on purpose.
4. **Modes.** Each agent has `mode`: `reactive` (may only emit `warn`) or `proactive` (may also
   emit `stop` / `propose_instruction`). `tier` decides approval: `autonomous` lets a `stop` run
   without a controller, `needs_approval` holds it for the owning controller. The gate enforces
   both. **Default (decided by the product owner, 13:55): stops are autonomous.** A critical
   stop executes and is spoken at once; the owning controller is told, not asked, and can lift
   it. Everything that is not a stop (proposed instructions, lane closures) still waits for the
   owning controller. Every decision, whoever made it, is an insert-only `decisions` document.
5. **Routing by ownership.** Every decision carries `lane_id` and `controller_id` = the owner
   of that lane (the supervisor if nobody owns it). Only that controller or a supervisor can
   approve, reject or revoke it.
6. **Access control, three layers.** (a) validators in Atlas (agent identity = its collection,
   never "clear"), (b) `holdshort/access.py` `ScopedDb`: every agent gets a database handle
   limited to the `read`/`write` lists in its permission document; a denied call raises and is
   written to `security_events`, (c) per-agent Atlas users generated on demand
   (`holdshort/identity/`): when Atlas admin credentials are present, each agent's custom role
   and short-lived database user are created from its permission document through the Atlas
   Admin API and the agent connects as that user; without the credentials it falls back to the
   shared connection string (or `MONGODB_URI_<AGENT>` if set).
7. **History for time travel.** `positions` (time-series collection) keeps every position.
   `state_events` (insert-only) records every change to lane status, permissions, controllers
   and rule sets. Current-state collections are overwritten; history is rebuilt from these two.
8. **Hold Short speaks only through the speaker service**, only for an executed `stop` or an
   approved `propose_instruction`, on `decision.stream_id`, and it is logged as a synthetic
   outbound transmission.

## 4. Processes and owners

| Process | Module / path | Entry |
|---|---|---|
| api | `holdshort/api/app.py` + `holdshort/api/routers/*.py` | `uvicorn holdshort.api.app:app --port 8000` |
| listener | `holdshort/listener/` | `python -m holdshort.listener` |
| state | `holdshort/state/` | `python -m holdshort.state` |
| memory | `holdshort/memory/` | `python -m holdshort.memory` |
| gate | `holdshort/gate/` | `python -m holdshort.gate` |
| safety agent | `holdshort/safety/`, `holdshort/agents/safety_agent.py` | `python -m holdshort.agents.safety_agent` |
| procedures agent | `holdshort/agents/procedures_agent.py` | `python -m holdshort.agents.procedures_agent` |
| weather agent | `holdshort/agents/weather_agent.py` | `python -m holdshort.agents.weather_agent` |
| speaker | `holdshort/speaker/` | `python -m holdshort.speaker` |
| everything in one container | `holdshort/serve.py` | `python -m holdshort.serve` |
| viz api | `visualizer/api/` | `uvicorn app:app --port 8100` (inside `visualizer/api`) |
| viz web | `visualizer/web/` | `npm run dev -- --port 5180` |

Every worker accepts `--db NAME`, resumes from a change-stream token it stores in
`agent_state` (`_id` = process name), logs to stdout, and exits cleanly on SIGTERM.

## 5. Rules for every task

- Stay inside the paths your task owns. `holdshort/contracts.py` and `scripts/setup_atlas.py`
  belong to the foundation task: if you truly need a new field, make the smallest edit and name
  it in your final answer. Do not rename anything in `CONTRACTS.md`.
- Commits: plain `git commit`. Never `-c user.*`, `--author`, `--no-verify` or git config
  changes: the queue sets the author and a hook adds the whole team as co-authors.
- Push your branch. No pull requests, no merges, never push to `main` or `v2/base`.
- Secrets load through `holdshort/env.py` from `~/plane-management-2/.env`. Never print, log or
  commit them. The repo is public.
- Tests use their own database: `holdshort_v2_t<task number>` (the number is in your branch
  name `hq/<number>-...`). Drop it when done. Never use `holdshort` or `holdshort_v2` in tests.
- Your own test servers use ports 18000-18999. Never 8000, 8100 or 5180. Stop everything you
  start.
- No sudo, with one exception: `sudo docker ...` in the docker_deploy and viz_integration tasks.
- No real ATC recordings, airline logos, crew names or crash imagery.
- MongoDB guidance: before writing schema, index, change-stream or search code, read the
  matching MongoDB agent skill in `/home/claude/plane-management/.claude/skills/`
  (`mongodb-schema-design`, `mongodb-search-and-ai`, `mongodb-query-optimizer`,
  `mongodb-connection`). `scripts/mcp-mongodb.sh` starts the MongoDB MCP server if you want to
  inspect data.
- ElevenLabs and LLM calls cost shared credits: unit tests must stub them. Only the smoke feed
  and the end-to-end check make real calls, and they cache generated audio in
  `tools/fixtures/audio/` (gitignored).
- External services must degrade: no `ELEVENLABS_API_KEY` / `VOICE_ENABLED` false -> use the
  clip's `truth_text` and a placeholder tone; no `LLM_API_KEY` -> rule/template fallback.
- Time box: about 15 minutes. A small working piece beats a large broken one.

## 6. Tasks

| Task | Owns | After |
|---|---|---|
| foundation | `holdshort/contracts.py`, `scripts/setup_atlas.py`, `holdshort/world/`, `holdshort/access.py`, `holdshort/events.py`, `holdshort/api/deps.py`, `holdshort/agents/_stream.py`, `tests/conftest.py`, removal of v1-only code | - |
| platform | `holdshort/api/app.py`, `holdshort/api/json.py`, `holdshort/api/routers/__init__.py`, `holdshort/serve.py` | - |
| ingest | `holdshort/api/routers/ingest.py`, `holdshort/ingest/` | foundation |
| listener | `holdshort/listener/` | foundation |
| state | `holdshort/state/` | foundation |
| memory | `holdshort/memory/` | foundation |
| safety | `holdshort/safety/`, `holdshort/agents/safety_agent.py` | foundation |
| gate_control | `holdshort/gate/`, `holdshort/api/routers/control.py` | foundation |
| advisor | `holdshort/agents/procedures_agent.py`, `holdshort/agents/weather_agent.py` | foundation |
| speaker | `holdshort/speaker/`, `holdshort/api/routers/outbound.py` | foundation |
| smoke_feed | `tools/smoke_feed.py`, `tools/fixtures/` | foundation |
| viz_api | `visualizer/api/` | - |
| viz_web | `visualizer/web/` | - |
| docker_deploy | `deploy/` | foundation, viz_api, viz_web |
| integration | `scripts/run_v2.sh`, `scripts/stop_v2.sh`, `scripts/e2e_check.sh`, seams between tower modules | all tower tasks |
| viz_integration | seams between visualizer and tower, `TESTING.md` | integration, viz_api, viz_web, docker_deploy |

## 7. Cut line

Must work: ingest of positions and of clips on 3 streams at once -> transcripts with
`stream_id` -> lane status -> a safety warning or stop routed to the owning controller ->
approve in the visualizer -> spoken outbound clip; expiring `context`; visualizer live view,
controller filter, stream selection with audio, time travel.

Only if the above works: LLM advisor proposals beyond the template, per-agent Atlas users,
cloud deployment, lane close/reopen.

## 8. Containers and hosting

- `deploy/backend.Dockerfile`: the tower (one image; `python -m holdshort.serve` runs every
  process; `--only api,gate` runs a subset).
- `deploy/viz-api.Dockerfile`, `deploy/viz-web.Dockerfile` (static build served by nginx).
- `deploy/docker-compose.yml`: the three services for a local run, `env_file` = the repo `.env`.
- Hosting targets: the two API containers on Google Cloud (Cloud Run: the tower needs
  always-on workers and WebSockets, so `--min-instances 1 --no-cpu-throttling`), the web app on
  Firebase Hosting. Firebase Hosting rewrites do not carry WebSockets, so the web app takes the
  API base URLs from `VITE_VIZ_API` and `VITE_TOWER_API` and connects to them directly.
- Nothing is deployed by a task. Deploy scripts are written and dry-checked only.
