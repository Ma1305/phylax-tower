# Testing Phylax Tower by hand

Three systems run side by side: the **tower** (agents, gate, speaker; port 8000), the
**visualizer API** (read-only; port 8100) and the **web app** (port 5180). The simulator, or
the small stand-in `scripts/feed.sh`, feeds the tower through `/ingest/*`.

## 1. Start

In the repository, with `MONGODB_URI` in `.env` (and a link to it at `visualizer/api/.env`, see
the README's quick start):

```bash
scripts/run_all.sh                 # database holdshort_v2; or: scripts/run_all.sh --db NAME
```

It runs `scripts/run_v2.sh` (collections, validators, world seed, every tower process), then the
visualizer API and the web dev server, waits until all three answer and prints the URLs.
Logs and pid files are in `logs/v2/` (`serve.log`, `viz_api.log`, `viz_web.log`).
Stop everything with `scripts/stop_all.sh`.

| What | URL |
|---|---|
| Web app | http://localhost:5180 |
| Visualizer API | http://localhost:8100/viz/health |
| Tower API | http://localhost:8000/health |
| Web app without any backend (scripted data) | http://localhost:5180/?mock=1 |

The browser talks to the visualizer API (data, audio, the live WebSocket) and to the tower API
(approve, reject, lift, switches, the audit question) directly. If the stack runs on another
machine, forward all three ports:
`ssh -N -L 5180:localhost:5180 -L 8100:localhost:8100 -L 8000:localhost:8000 HOST`.

Feed the tower from a second terminal:

```bash
scripts/feed.sh incident           # normal | incident | intruder | both   [--loop] [--speed 2]
```

Each run first calls `/ingest/reset` (live picture only; the permanent log is kept), so you can
run scenarios one after another. With `VOICE_ENABLED=false` the clips are tones of the right
length and the transcripts come from the feed's text; with voice on they are real synthetic
speech (ElevenLabs credits).

## 2. Two-minute click-through: the incident

Start with "View as" on **All controllers** and the safety agent on proactive / autonomous (the
default; the top bar says "Agents on · safety autonomous"). Run `scripts/feed.sh incident`
(75 seconds).

| When | What to do | What to expect |
|---|---|---|
| 0 s | **Approach** above the map | AC8646 on the final for runway 04, descending. The chip at the top right of the map shows its distance. **Airfield** goes back. |
| ~10 s | nothing | Tower clears AC8646 to land: the calls appear in **Radio** and on the TWR row of the streams dock, and runway 04/22 turns **RESERVED**. TRUCK1 waits on taxiway Delta. An unclear call shows with an **Unclear** tag. |
| ~25 s | nothing | Ground clears TRUCK1 to cross runway 04 at Delta. Runway 04/22 turns **CONFLICT**. |
| ~30 s | nothing | **The stop is autonomous: you are told, not asked.** A red chip on the map says "Stop in effect" and **Needs you** shows a **STOP IN EFFECT** card: the exact call ("Truck one, STOP. Hold short runway zero four."), "Issued by Phylax to TRUCK1", the controller who was told, and **Lift stop** and **Focus conflict**. The same call appears in Radio tagged **Phylax**. TRUCK1 stops at the hold line; AC8646 lands. |
| any time | **▶** on a call in Radio, or a clip in the streams dock | The clip plays. **Listen live** on a stream's row plays its new calls as they arrive. |
| after the stop | "View as" **Local (Tower)** | Local's lanes and streams, the stop, and one **PROPOSED INSTRUCTION** from the procedures agent, routed to Local because Local owns runway 04. "View as" **Ground** shows neither. |
| | "View as" **All controllers**, "Act as" **Ground**, **Approve** | A red line under the card: `403 · not_lane_owner`. The tower also writes a `security_events` document. |
| | "Act as" **Local (Tower)**, **Approve** | The card disappears and the instruction is spoken: a new **Phylax** call in Radio, with audio. |
| | **Lift stop** (as Local or Supervisor) | The stop card disappears. |

**Audit log** (top right) lists every gate decision, newest first: **■ STOP EXECUTED**,
**… PENDING**, **✓ EXECUTED**, **⊘ REFUSED BY GATE**, **✕ REJECTED**, **↺ STOP LIFTED**,
**△ ADVISORY**. Each line shows the rule (the agent's reason and the gate's reason code),
`authorized by` (permission document and rule set version) and who approved, rejected or
lifted it.

## 3. Ask the audit agent

At the top of the **Audit log**, type a question into **Ask the audit agent** (it is pre-filled
with "Why was TRUCK1 held?") and press **Ask**. The web app calls the tower's
`GET /control/audit?q=...`. You get a short narrative that cites record ids in square brackets,
and under it the timeline it was built from (transmissions, lane status changes, intents,
decisions with rule, authorized_by and approved_by); the cited entries are marked with ★. It
only reads stored records, so it also works in history mode. Other questions that work: "What
happened on runway 04?", "What did Local (Tower) approve?".

## 4. Agent switches

**Settings** (top right). Changes are sent as the controller chosen in "Act as" (or the one
chosen in "View as"), are logged in `state_events`, and apply to the agent's next intent.

- **Autonomous or needs approval.** Set **Safety** to tier **needs approval** and run
  `scripts/feed.sh incident` again. This time the stop waits in **Needs you** as
  **STOP PROPOSED**, nothing is spoken, and TRUCK1 crosses if you do nothing. **Approve** as
  Local (Tower): the stop takes effect and the call is spoken. **Reject** instead logs
  **✕ REJECTED**. Set the tier back to **autonomous** afterwards.
- **Reactive or proactive.** A **reactive** agent may only warn: with Safety on reactive the
  incident produces an advisory and no stop. **proactive** lets it stop or propose.
- **Off.** Switch an agent off (or the **Master switch**, which holds all of them): its next
  intent is refused by the gate and logged as **⊘ REFUSED BY GATE** with rule `agent_disabled`.
- **Identity.** Under each agent's name: how its process connected to Atlas. `Atlas user <name>`
  is a database user generated from the agent's permission document, `static Atlas user` is
  `MONGODB_URI_<AGENT>`, and `shared connection` is the common connection string.

## 5. Time travel and highlights

Drag anywhere on the timeline at the bottom. The frame turns amber, a banner says
"Viewing HH:MM:SS · not live · actions disabled", and every action button is disabled. The map,
lane states, radio, pending requests, stops and the audit log are rebuilt from the stored
records for that moment (`positions`, `state_events`, `transmissions`, `decisions`).

- Drag to just before the crossing clearance: runway 04/22 is **RESERVED**, not in conflict,
  TRUCK1 is back on taxiway D, AC8646 is back on final, and there is no stop.
- Play forward from there at 1x, 4x or 16x; **Skip quiet gaps** jumps over stretches with no
  call, decision or alert. A stream set to listen plays its clips at their original times.
- **Highlights** steps through the major events of the window (stops, proposals, conflicts,
  the start of each run), each from a few seconds before to a few seconds after.
- The marks on the timeline are transmissions, advisories, stops and proposals, and switch
  changes. The streams dock above it shows each stream's clips on the same time axis.
- **LIVE**, or **Back to live** in the banner, returns to now.

## 6. The other scenarios

From `scripts/feed.sh` (the small stand-in feed):

- `normal` (62 s): a landing on 04, a taxi on Bravo and an apron inspection, with requests,
  clearances and readbacks on all three streams. A correct system stays quiet: **no stop**.
- `intruder` (70 s): AC8646 is cleared to land on 04 and an unidentified person (no callsign,
  no radio) walks onto the runway with no clearance. Runway 04/22 turns **CONFLICT** and the
  stop is issued at once, rule `runway_incursion`. The person has no radio, so nothing obeys
  it: the point is that the controller is told.
- `both`: the normal run followed by the incident. Add `--loop` to keep it running.

From the simulator (realistic traffic; see [`simulator/README.md`](simulator/README.md)):

```bash
cd simulator && uv sync
uv run python -m hssim list                                        # every scenario and what the tower should do
uv run python -m hssim run laguardia --api http://localhost:8000   # the 22 March 2026 reconstruction
uv run python -m hssim run crossing  --api http://localhost:8000   # a crossing cleared during a landing
```

The simulator talks to the tower only over HTTP, as described in
[`docs/v2/SIMULATOR_CONTRACT.md`](docs/v2/SIMULATOR_CONTRACT.md), and its traffic obeys what the
tower says: a target that receives a stop, stops.

## 7. Automated checks

```bash
uv run ruff check .                              # lint
uv run pytest -q                                 # the tower's tests; need MONGODB_URI, use a throwaway database
(cd simulator && uv run pytest -q)               # the simulator against its own stand-in tower; no database
(cd visualizer/web && npm ci && npm run build)   # type-check and build the web app
scripts/e2e_check.sh                             # the tower end to end: three incident runs and a normal run
```

`scripts/e2e_check.sh` uses a throwaway database and a free port, plays the controllers
through `/control/*`, and after each run audits the decision log: one decision per intent plus
one per approval, rejection or revocation, and no outbound call without an executed decision.

The two Playwright runs of the web app (`scripts/viz_integration.sh` against the real tower,
`npm run pair` in `visualizer/web` against a seeded visualizer API) were written for the screen
as it was before its final redesign and have not been updated for the current layout.

## 8. If something looks wrong

- The page says the visualizer API is not answering: `scripts/run_all.sh` is not running, or
  the ports are not forwarded. `curl localhost:8100/viz/health`.
- "Tower API not reachable" under a button: the tower stopped or port 8000 is not reachable.
  `curl localhost:8000/health` and `tail logs/v2/serve.log`.
- The pill at the top says **FEED LOST**: the WebSocket to the visualizer API dropped; it
  reconnects by itself.
- Nothing moves: no feed is running. `scripts/feed.sh incident`.
- A stop from an earlier run is still shown: press **Lift stop**.
