# Phylax Tower: design

This began as the build plan for the second version of the system ("v2") and is kept as the
design document. It describes the system as it was built. Phylax Tower was called Hold Short
during the build, so identifiers (the `holdshort` package, the `holdshort_v2` database, the
`HOLDSHORT` station) keep the old name.

Read this first, then `CONTRACTS.md` (data shapes and APIs). `SIMULATOR_CONTRACT.md` is the
only source for the `/ingest/*` API. `world_lga.json` is the seeded airport.

Hackathon: MongoDB "Agents in Context" (Toronto, 14 November 2026), statement #1 "Agents You
Can Audit". Everything lives in one MongoDB Atlas cluster, database `holdshort_v2`.

## 1. Product

Three separate systems:

1. **Tower (main system, package `holdshort/`)**. One tower, several controllers, each owning
   lanes (runways, taxiways, aprons). It takes in many radio streams and position feeds at the
   same time, keeps a live picture (objects, lane status), keeps a small expiring working memory
   for the agents, and runs agents that warn, stop a conflict at once (a critical stop is
   autonomous by default: the owning controller is told and can lift it), or propose an
   instruction that the owning controller approves.
2. **Visualizer (`visualizer/`)**. Separate system: its own read-only API and its own web app.
   It never imports from `holdshort/` and never writes to the database. Controller actions go
   to the tower's `/control/*` API.
3. **Simulator (`simulator/`)**. A separate program that feeds the tower through `/ingest/*`
   only: traffic, radio and weather scenarios, including the LaGuardia reconstruction.
   `tools/smoke_feed.py` is a small stand-in used by the tests.

Core rule, unchanged: **the AI can warn, stop or propose. It can never clear traffic. The
controller stays in charge.**

### What the judges asked for (statement #1), and where we answer it

The statement: use Atlas as the identity, policy and audit layer (collections, schema
validation, change streams) for an agent that takes real actions, where every action is tied to
an identity, checked against policy before it runs, and recorded so a reviewer can reconstruct
it afterward. The three answers are visible in the code, the data and the screen:

| Their question | Our answer |
|---|---|
| How do you decide what an agent may do and enforce it before the action? | Agents only insert intents. Atlas validators, the `ScopedDb` access lists and the gate (mode, tier, lane owner) all run before any decision executes. |
| How do you record what it did, why, and under whose authority? | Insert-only `decisions` with `authorized_by`, `controller_id`, `approved_by`; `security_events` for every blocked attempt; `state_events` for every switch change. |
| How do you stop or roll back an agent mid-run? | Per-agent and master switches, mode and tier changes take effect on the next intent; `revoke` inserts a new decision pointing at the old one. |

The visualizer's time travel is the "reviewer reconstructs it afterward" feature: it is built
from the stored records only.

The hackathon rules ruled out basic RAG, Streamlit, and projects where a dashboard is the main
feature, so the tower and its agents are the product and the visualizer is a separate viewer.

Technology, used where it fits: Atlas schema validation, change streams, time-series and TTL
collections, Atlas Search / Vector Search with automated (Voyage) embeddings for the agents'
working memory and similar incidents; LangGraph with MongoDB checkpoints (plus LangSmith
tracing when `LANGSMITH_API_KEY` is set) for the LLM advisor; Gemini on Vertex AI as the
language model (`holdshort/llm.py`; an Anthropic provider is wired in as an alternative, and
with no provider the agents fall back to rules and templates); ElevenLabs for speech-to-text,
voice isolation and the spoken outbound calls.

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
| web (React + Vite :5180)        map, radio, streams dock, timeline      |
+-------------------------------------------------------------------------+
```

## 3. Design decisions

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
   both. **By default stops are autonomous.** A critical
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
   (`holdshort/identity/`, off by default): with `AGENT_IDENTITIES=generated` and Atlas admin
   credentials, each agent's custom role and short-lived database user are created from its
   permission document through the Atlas Admin API and the agent connects as that user;
   otherwise it uses the shared connection string (or `MONGODB_URI_<AGENT>` if set).
7. **History for time travel.** `positions` (time-series collection) keeps every position.
   `state_events` (insert-only) records every change to lane status, permissions, controllers
   and rule sets. Current-state collections are overwritten; history is rebuilt from these two.
8. **Phylax Tower speaks only through the speaker service**, only for an executed `stop` or an
   approved `propose_instruction`, on `decision.stream_id`, and it is logged as a synthetic
   outbound transmission.

## 4. Processes

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

## 5. How it was built

In one day, by many small tasks running in parallel on a shared machine, each owning a few
paths (one per row of the table in section 4, plus the shared contracts, the database setup,
the smoke feed, the deploy files and the integration scripts). Three rules from that process
still shape the code:

- External services must degrade. No ElevenLabs key or `VOICE_ENABLED` false: the listener uses
  the text the feed sent with the clip and the speaker stores a placeholder tone. No language
  model provider: rule and template fallbacks.
- Tests never use the databases `holdshort` or `holdshort_v2`; each run creates and drops its
  own. Unit tests stub ElevenLabs and the model; only the proof scripts make real calls.
- No real ATC recordings, airline logos or crew names anywhere in the repository.

## 6. Scope

The core path: ingest of positions and of clips on three streams at once -> transcripts with
`stream_id` -> lane status -> a safety warning or stop routed to the owning controller ->
spoken outbound clip (a proposal first waits for approval in the visualizer); expiring
`context`; the visualizer's live view, controller filter, streams with audio and time travel.

Built on top of that: LLM advisor proposals beyond the template, per-agent Atlas users
(`holdshort/identity/`), the cloud deployment, lane close and reopen in the gate, the audit
agent, and in the visualizer the streams dock, highlights playback and the settings panel.

## 7. Containers and hosting

- `deploy/backend.Dockerfile`: the tower (one image; `python -m holdshort.serve` runs every
  process; `--only api,gate` runs a subset).
- `deploy/viz-api.Dockerfile`, `deploy/viz-web.Dockerfile` (static build served by nginx).
- `deploy/docker-compose.yml`: the three services for a local run, `env_file` = the repo `.env`.
- Hosting targets: the two API containers on Google Cloud (Cloud Run: the tower needs
  always-on workers and WebSockets, so `--min-instances 1 --no-cpu-throttling`), the web app on
  Firebase Hosting. Firebase Hosting rewrites do not carry WebSockets, so the web app takes the
  API base URLs from `VITE_VIZ_API` and `VITE_TOWER_API` and connects to them directly.
- The live demo's web app is at https://phylax-tower.web.app. Deploying is always a manual
  step (`deploy/README.md`).
