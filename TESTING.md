# Testing Hold Short v2 by hand

Three systems run side by side: the **tower** (agents, gate, speaker; port 8000), the
**visualizer API** (read-only; port 8100) and the **visualizer web app** (port 5180). The
simulator, or our small stand-in `scripts/feed.sh`, feeds the tower through `/ingest/*`.

## 1. Start

On the VM, in the repository (secrets come from `~/plane-management-2/.env`):

```bash
scripts/run_all.sh                 # database holdshort_v2; or: scripts/run_all.sh --db NAME
```

It runs `scripts/run_v2.sh` (collections, validators, world seed, every tower process), then the
visualizer API and the web dev server, waits until all three answer and prints the URLs.
Logs and pid files are in `logs/v2/` (`serve.log`, `viz_api.log`, `viz_web.log`).
Stop everything with `scripts/stop_all.sh`.

On your laptop, open the tunnel and leave it running:

```bash
ssh -N -L 5180:localhost:5180 -L 8100:localhost:8100 -L 8000:localhost:8000 hack-vm
```

| What | URL |
|---|---|
| Web app | http://localhost:5180 |
| Visualizer API | http://localhost:8100/viz/health |
| Tower API | http://localhost:8000/health |
| Web app without any backend (scripted data) | http://localhost:5180/?mock=1 |

All three ports must be forwarded: the browser talks to the visualizer API (data, audio, live
WebSocket) and to the tower API (approve, reject, lift, switches, audit question) directly.

Feed the tower from a second terminal on the VM:

```bash
scripts/feed.sh incident           # normal | incident | intruder | both   [--loop] [--speed 2]
```

Each run first calls `/ingest/reset` (live picture only; the permanent log is kept), so you can
run scenarios one after another. With `VOICE_ENABLED=false` the clips are tones of the right
length and the transcripts come from the feed's text; with voice on they are real synthetic
speech (ElevenLabs credits).

## 2. Two-minute click-through: the incident

Start with the view on **All** and the safety agent on **proactive / autonomous** (the default).
Run `scripts/feed.sh incident` (75 seconds).

| When | What to press | What to expect |
|---|---|---|
| 0 s | **Approach · 7 nm** above the map | AC8646 on the final for runway 04, one mile out, descending. The "Airborne" box lists it with distance, altitude and speed. Press **Airfield** to go back. |
| ~10 s | nothing | Tower clears AC8646 to land: three lines appear in the **Tower** stream on the right, runway 04/22 turns **RESERVED**. TRUCK1 waits on taxiway Delta (**OCCUPIED**). An unclear call shows in **Airport Ops** with an "unclear - say again" tag. |
| ~25 s | nothing | Ground clears TRUCK1 to cross runway 04 at Delta. Runway 04/22 turns **CONFLICT** with the reason under its label. |
| ~30 s | nothing | **The stop is autonomous: you are told, not asked.** A red **STOP IN EFFECT** bar appears at the top at once: "issued by Hold Short · not a request", the exact call ("Truck one, STOP. Hold short runway zero four."), target TRUCK1, rule `no_crossing_during_active_landing`, "Told Local (Tower)" and one button, **Lift stop**. The same call appears in the **Ground** stream marked **◆ HOLD SHORT · OUTBOUND** (Hold Short's own voice). TRUCK1 stops at the hold line and stays on taxiway D; AC8646 lands. |
| any time | **▶** on a line in the right bar | The clip plays in the player at the bottom of the bar. Pick **TOWER / GROUND / OPS** at the top of the bar to solo a stream: its new calls then play by themselves. |
| after the stop | **Local (Tower)** in "View as" | Only the Tower stream, the two runways highlighted, the stop bar and one request under **Awaiting approval**: the procedures agent's **PROPOSED INSTRUCTION** ("Truck one, hold short of runway zero four, expect to cross after the landing ... has vacated"), routed to Local (Tower) because Local owns runway 04. **Ground** shows two streams and neither the stop nor the request. |
| | **All**, then "act as" **Ground**, **✓ Approve** | A red line under the request: `403 · not_lane_owner: ctl_ground may not approve decision ... (routed to ctl_local)`. The tower also writes a `security_events` document. |
| | "act as" **Local (Tower)**, **✓ Approve** | The request disappears, the instruction is spoken: a new **◆ HOLD SHORT · OUTBOUND** line in the Ground stream with audio. The decision log shows it as **✓ EXECUTED**, approved by Local (Tower). |
| | **Lift stop** on the red bar (as Local or Supervisor) | The bar disappears; the log gets **↺ STOP LIFTED**, lifted by Local (Tower). |

The **Decision log** (middle column) lists every gate decision, newest first: executed,
pending, refused by the gate, rejected or lifted by a controller. Each line shows the rule (the
agent's reason and the gate's reason code), `authorized by` (permission document / rule set
version) and who approved, rejected or lifted it.

## 3. Ask the audit agent

In the **Awaiting approval** panel, under the requests, type a question into
**Ask the audit agent** (it is pre-filled with "Why was TRUCK1 held?") and press **Ask**. The
web app calls the tower's `GET /control/audit?q=...`. You get a short narrative that cites
record ids in square brackets, and under it the timeline it was built from (transmissions, lane
status changes, intents, decisions with rule, authorized_by and approved_by); the cited entries
are marked with ★. It only reads stored records, so it also works in history mode. Other
questions that work: "What happened on runway 04?", "What did Local (Tower) approve?".

## 4. Agent switches

Bottom of the middle column. Changes are sent as the controller chosen in "act as" (or the one
chosen in "View as") and are logged in `state_events`; they apply to the agent's next intent.

- **Autonomous vs needs approval.** Set **safety** to **needs approval** and run
  `scripts/feed.sh incident` again. This time no red bar appears: the stop waits under
  **Awaiting approval** as **STOP · NEEDS YOUR APPROVAL**, nothing is spoken, and TRUCK1 crosses
  if you do nothing. **✓ Approve** as Local (Tower): the red bar appears with "approved by Local
  (Tower)" and the call is spoken. **✕ Reject** instead logs **✕ REJECTED**. Set safety back to
  **autonomous** afterwards.
- **Reactive vs proactive.** A **reactive** agent may only warn: with safety on **reactive** the
  incident produces an advisory in the decision log (**△ ADVISORY**) and no stop; with
  procedures on reactive its proposal becomes an advisory. **proactive** lets it stop or propose.
- **Off.** Switch an agent off (or the **MASTER** switch, which holds all of them): its next
  intent is refused by the gate and logged as **⊘ REFUSED BY GATE** with rule `agent_disabled`.
- **Identity.** Under each agent's name: how its process is connected to Atlas, when the tower
  has recorded it in `agent_state`: `Atlas user <name>` (a database user generated on demand
  from the agent's permission document; hover for role and expiry), `static Atlas user`
  (`MONGODB_URI_<AGENT>`) or `shared connection`. No line means the tower has not recorded one.

## 5. Time travel

Drag anywhere on the timeline at the bottom. The frame turns amber, a banner says
**HISTORY · NOT LIVE** with the time you are looking at, and every action button is disabled.
The map, lane states, streams, pending requests, stop bar and decision log are rebuilt from the
stored records for that moment (`positions`, `state_events`, `transmissions`, `decisions`).

- Drag to just before the crossing clearance: runway 04/22 is **RESERVED**, not in conflict,
  TRUCK1 is back on taxiway D before the hold line, AC8646 is back on final, no stop bar, an
  empty decision log.
- **▶** plays forward from there (1x, 4x, 16x); with a stream soloed its clips play at their
  original times at 1x.
- The marks on the timeline are transmissions per stream, advisories, stops / proposals and
  switch changes; dotted lines are the start of each feed run.
- **LIVE** (bottom left) or **Back to LIVE** in the banner returns to now.

## 6. The other scenarios

- `scripts/feed.sh normal` (62 s): a landing on 04, a taxi on Bravo and an apron inspection,
  with requests, clearances and readbacks on all three streams. Runway 04/22 goes reserved, then
  occupied, then clear. A correct system stays quiet: **no stop**, no request.
- `scripts/feed.sh intruder` (70 s): AC8646 is cleared to land on 04 and an unidentified person
  (OBJ-17, no callsign) walks onto the runway with no clearance. Runway 04/22 turns **CONFLICT**,
  the log gets an advisory and the red bar appears at once: "Person on runway zero four, STOP.
  Vacate the runway immediately.", rule `runway_incursion`, told Local (Tower). The person has
  no radio, so nothing obeys it: the point is that the controller is told. A proposal from the
  procedures agent follows under **Awaiting approval**; its wording for a target with no
  callsign is clumsy (it spells out "UNKNOWN"), reject it.
- `scripts/feed.sh both`: the normal run followed by the incident. Add `--loop` to keep it
  running for a demo (Ctrl-C, or `scripts/stop_all.sh`, stops it).

## 7. How the simulator plugs in

The simulator replaces `scripts/feed.sh`; nothing else changes. It talks to the tower only over
HTTP, as described in [`docs/v2/SIMULATOR_CONTRACT.md`](docs/v2/SIMULATOR_CONTRACT.md):

- `GET /ingest/world` for the airport geometry and ids, `POST /ingest/reset` before a scenario,
  `POST /ingest/runs` to mark a run on the timeline;
- `POST /ingest/positions` about once a second, `POST /ingest/audio` (one clip per radio call,
  tagged with its `stream_id`) or `POST /ingest/transcripts` for text-only calls,
  `POST /ingest/weather`;
- `GET /ingest/outbound?tower_id=LGA&since=ISO` about once a second to hear what Hold Short
  said: a target that receives `stop` should stop moving. Deduplicate by `decision_id`, and
  keep `since` a few seconds behind the returned `now`: a call's `t` is when it was decided,
  which can be slightly earlier than the moment it becomes visible.

Point the simulator at `http://localhost:8000` (or the deployed tower URL) and watch the web app.

## 8. Automated checks

```bash
scripts/viz_integration.sh    # the web app against the real tower, with Playwright (about 5 min)
scripts/e2e_check.sh          # the tower alone: three incident runs and a normal run
(cd visualizer/web && npm run pair)   # the web app against a seeded visualizer API
```

`scripts/viz_integration.sh` uses a throwaway database (`holdshort_v2_t<task>_viz`) and free
ports in 18000-18999, runs the incident twice (autonomous, then needs approval), clicks through
everything in sections 2 to 5, saves a screenshot of each step in `docs/v2/screens/`, then
stops everything and drops the database.

## 9. If something looks wrong

- Boot page "The visualizer API ... is not answering": the tunnel is down or
  `scripts/run_all.sh` is not running. `curl localhost:8100/viz/health`.
- "Tower API not reachable" under a button: port 8000 is not forwarded, or the tower stopped.
  `curl localhost:8000/health` and `tail logs/v2/serve.log`.
- Pill says **FEED LOST · RETRYING**: the WebSocket to port 8100 dropped; it reconnects itself.
- Nothing moves: no feed is running. `scripts/feed.sh incident`.
- A stop bar from an earlier run is still there: stops stay shown for ten minutes unless
  lifted. Press **Lift stop**.
