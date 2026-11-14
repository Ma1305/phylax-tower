# Hold Short · web

The demo screen: a top-down LaGuardia schematic (runway 04/22, runway 13/31,
taxiway D with its hold-short line), jet AC8646 on final to 04, the TRUCK1
convoy at the D hold-short line, plus radio, alert and Atlas-event panels.
React + Vite + TypeScript, no UI libraries.

```bash
cd web
npm install
npm run dev          # http://localhost:5173
npm run build        # type-check + production build into dist/
```

From your laptop, tunnel the VM's dev server and open http://localhost:5173
(or http://localhost:5173/?mock=1):

```bash
ssh -N -L 5173:localhost:5173 hack-vm
```

## Modes

* **Live** (default): WebSocket `ws://localhost:8000/ws`, REST on
  `http://localhost:8000`. Override with `VITE_WS_URL` / `VITE_API_BASE`
  (see `.env.example`) or per page load with `?ws=...&api=...`.
* **Mock** `?mock=1` (optionally `&speed=4`): replays
  `src/mock/laguardia.json` in the browser. No backend needed.
  * Safety ON (default): STOP fires at 23:36:47, trucks hold short, lights
    go red, AC8646 lands, the procedures proposal appears (Approve/Reject
    work; it auto-approves at 23:37:04 if nobody clicks), the convoy crosses
    after the jet passes D.
  * Switch **Safety** off before **Play**: no alert, the convoy rolls onto
    the runway and the picture freezes before contact (no crash imagery).
  * Switch **Procedures** off mid-run: its proposal comes back
    `rejected · agent_disabled`.
  * **Rogue agent**: shows Atlas rejecting a `"clear"` intent.

## Wire contract used

Incoming WS frames: `{type: "transmission"|"decision"|"permission"|"state"|"snapshot", doc}`.
`state` docs with `callsign` are treated as `flight_state`, all others as `runway_status`.
`snapshot.doc` = `{transmissions, decisions, permissions, flight_state, runway_status}`.

Outgoing:

| control | request |
|---|---|
| agent toggles + Master | WS `{type: "set_permission", agent, enabled}` (falls back to `POST /permissions/{agent}` `{enabled, by}` if the socket is down) |
| Approve / Reject | `POST /approve` `{decision_id, approved_by, approve: true\|false}` |
| Lift stop | `POST /revoke` `{decision_id, by}` |
| Play / Reset / Rogue | `POST /scenario/start`, `POST /scenario/reset`, `POST /rogue` |

The request bodies are a best guess until the api task lands. They all live
in one place (`src/feed.ts`, `LiveFeed`).

## How the map moves

`src/sim.ts` works out positions from the event log and the scenario time
alone:

* The jet passes D 98 s after its landing clearance.
* The convoy starts 2 s after its crossing clearance.
* An executed stop decision whose `notified` contains `TRUCK1` holds the convoy
  at the line.
* An approved `propose_instruction` (or a revoke of the stop) lets the convoy
  cross once the jet is past D.

Rewinding just clears the log and plays it again.
