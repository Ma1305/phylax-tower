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

Decision frames carry `intent_action` plus a read-only `intent` view
(`{action, target, runway, reason, conflicts_with, proposal_text}`) added by the
API; `normDecision()` in `src/feed.ts` maps them onto the UI's fields.

Outgoing (all REST; the change stream echoes the result back over the WS):

| control | request |
|---|---|
| agent toggles + Master | `POST /permissions/{agent}` `{enabled, by: "controller_A"}` |
| Approve | `POST /approve` `{intent_id, controller}` |
| Reject | `POST /revoke` `{decision_id: <pending decision>, controller}` (the gate has no reject; a revoked pending decision can't be approved) |
| Lift stop | `POST /revoke` `{decision_id, controller}` |
| Play | `POST /scenario/start` `{safety_enabled: <Safety toggle>, mode: "text"}` |
| Reset / Rogue | `POST /scenario/reset`, `POST /rogue` (-> `{ok, result: [security_event…]}`) |

## How the map moves

`src/sim.ts` works out positions from the event log and the scenario time
alone:

* The jet passes D 63 s after its landing clearance live (the script's
  `conflict_offset_s`), 98 s in the mock replay.
* The convoy starts 2 s after its crossing clearance.
* An executed stop decision whose `notified` contains `TRUCK1` holds the convoy
  at the line.
* An approved `propose_instruction` (or a revoke of the stop) lets the convoy
  cross once the jet is past D.

Rewinding just clears the log and plays it again.
