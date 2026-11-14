# Hold Short

An airport tower assistant built on MongoDB Atlas. Agents listen to several radio streams and
position feeds at once, keep a live picture of every runway and taxiway, and warn, stop or
propose an instruction to the controller who owns that lane. **The AI can warn, stop or
propose. It can never clear traffic. The controller stays in charge.**

All radio in this repo is synthetic (text-to-speech or tones). No real ATC recordings.

## Three systems

| System | Where | What it does |
|---|---|---|
| Tower (the product) | `holdshort/` | FastAPI `/ingest/*`, `/control/*`, `/health` plus workers: listener (audio clips -> transmissions), state (lane status), memory (expiring `context`), agents (safety rules, procedures LLM advisor, weather), gate (intents -> decisions), speaker (spoken outbound calls). |
| Visualizer | `visualizer/` | Separate read-only API and web app: live map, streams, controllers, time travel from stored records. Never writes; controller actions go to the tower's `/control/*`. |
| Simulator | `simulator/` (separate build) | Feeds the tower through `/ingest/*` only. `tools/smoke_feed.py` is a small stand-in for tests. |

## Run it

Requirements: Python 3.12 with [uv](https://docs.astral.sh/uv/), `ffmpeg` (placeholder tones),
an Atlas cluster. Put `MONGODB_URI` (and optionally `LLM_API_KEY`, `ELEVENLABS_API_KEY`,
`VOICE_ENABLED`) in a gitignored `.env`; copy `.env.example`. Without the optional keys the
tower degrades: rule/template text instead of the LLM, `truth_text` and tones instead of speech.

```bash
uv sync
scripts/run_v2.sh --db holdshort_v2 --port 8000     # setup_atlas, setup_search (bg), serve; waits for /health
uv run python tools/smoke_feed.py --api http://localhost:8000 --scenario incident
scripts/stop_v2.sh
```

`python -m holdshort.serve` runs every process in one supervisor (`--only api,gate` for a
subset); logs go to `logs/v2/serve.log`. Each worker can also run alone (`python -m
holdshort.gate --db NAME`, ...).

Checks:

```bash
uv run pytest -q && uv run ruff check .
scripts/e2e_check.sh     # throwaway db + free port: autonomous, needs_approval, reactive runs
uv run python scripts/voice_proof.py   # real ElevenLabs STT + TTS once (4 calls, needs VOICE_ENABLED)
```

Stops are autonomous by default (safety proactive / autonomous): a critical stop executes and
is spoken at once and is routed to the lane's controller, who can lift it (`/control/revoke`).
Proposed instructions and lane closures always wait for that controller.
`GET /control/decisions` is the decision log, newest first, each with its intent.

## Contracts

- `docs/v2/PLAN.md`: product, architecture, processes, design decisions.
- `docs/v2/CONTRACTS.md`: every collection shape, enum and the `/control/*` and `/viz/*` APIs
  (`holdshort/contracts.py` and the validators in `scripts/setup_atlas.py` match it).
- `docs/v2/SIMULATOR_CONTRACT.md`: the `/ingest/*` API.

## The three questions (statement #1, "Agents You Can Audit")

| Question | Answer in this build |
|---|---|
| How do you decide what an agent may do, and enforce it before the action? | Agents only insert intents into their own `intents_<agent>` collection. Atlas `$jsonSchema` validators (agent identity, no "clear" action), the `ScopedDb` read/write lists from `permissions`, and the gate (enabled, mode, tier, lane owner) all run before any decision executes. |
| How do you record what it did, why, and under whose authority? | Insert-only `decisions` carry `rule`, `authorized_by`, `controller_id`, `approved_by`; every blocked attempt (wrong controller, denied read, validator rejection) lands in `security_events`; every switch, lane and rule-set change in `state_events`. Transmissions are never deleted. |
| How do you stop or roll back an agent mid-run? | Per-agent and master switches, mode (`reactive` = warn only) and tier take effect on the next intent (`POST /control/permissions/{agent}`); `POST /control/revoke` inserts a new decision pointing at the old one. |

`scripts/e2e_check.sh` exercises all three: an autonomous stop executed and spoken within 3 s
of the conflict, routed to the runway's controller, holding the truck short; the resume
proposal waiting for that controller (a 403 plus a security event when the ground controller
tries to approve it); the same incident with tier needs_approval (the stop waits for the
controller) and with safety reactive (only an advisory). After each run it audits the decision
log: one decision per intent plus one per approval / rejection / revocation, and no outbound
call without an executed decision.
