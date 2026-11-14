# Hold Short: live demo runbook

Everything below runs against Atlas for real: no mock. (`?mock=1` is still there as a
no-backend fallback, see "If something breaks".)

## 0. Before the demo (VM)

```bash
cd ~/plane-management
scripts/demo.sh            # Atlas setup, gate, safety/procedures/weather agents, API :8000, web :5173
```

`scripts/demo.sh` returns once everything is up. It runs `scripts/setup_atlas.py`
(idempotent), then sets every switch (system, listener, weather, safety, procedures) ON,
logged as `by: demo_setup`. Logs: `logs/demo/*.log`. Stop everything with
`scripts/demo_stop.sh`.

On the laptop that shows the screen:

```bash
ssh -N -L 5173:localhost:5173 -L 8000:localhost:8000 hack-vm
```

then open **http://localhost:5173/**. The clock under the title reads
`scenario time (UTC) · live` once the WebSocket is connected.

- Playback is real time (~80 s per run, scenario 23:36:30 to 23:37:48 UTC). To rehearse
  faster: start with `HOLDSHORT_SCENARIO_SPEED=5 scripts/demo.sh` and open
  `http://localhost:5173/?speed=5` (the page clock must use the same speed).
- **Play** = `POST /scenario/start {safety_enabled: <Safety toggle>}`. It clears the live
  state (flight_state, runway_status), sets the safety switch to match the toggle
  (logged, `by: controller_A`) and plays the script. **Reset** = `POST /scenario/reset`:
  cancels a run in progress and clears the screen. Audit data is never deleted.
- Only one run plays at a time: press **Reset** before pressing **Play** again.

Timeline of one run (scenario time): 23:36:40 AC8646 cleared to land runway 04 ·
23:37:18 TRUCK1 cleared to cross runway 04 at D · STOP fires immediately when Safety
is on · 23:37:38 the tower's own "Stop, stop, stop!" · AC8646 reaches D at about 23:37:43.

## 1. Safety OFF: no alert, freeze

1. Click the **Safety** toggle so it reads **OFF** (top right). Chip on the map:
   `Live · safety agent off`. Atlas strip shows `permissions update {agent: "safety", enabled: false, by: "controller_A"}`.
2. Click **▶ Play**.
3. Radio fills in: landing clearance, crossing request, the unclear call ("say again",
   amber badge), crossing clearance and readback.
4. Alerts panel: only a grey `✕ stop runway 04` card (`safety · stop · rejected`,
   `rule: agent_disabled`): the safety agent saw the conflict but its switch is off, so
   the gate refused it. Nothing reaches the trucks.
5. The convoy rolls onto runway 04, the picture **freezes** before the jet reaches D:
   "No alert fired. Both clearances stand." Stop talking here.

## 2. Rewind, Safety ON: STOP fires, trucks hold

1. Click **⟲ Reset** (screen clears; the run's records stay in Atlas).
2. Click the **Safety** toggle so it reads **ON** (logged permission change, shows in the
   Atlas strip).
3. Click **▶ Play**. Same calls.
4. At 23:37:18, the crossing clearance: red **⛔ STOP** card "Runway 04 — AC8646 cleared to
   land", a `SAFETY · AUTO` line in the radio panel ("STOP. Hold short runway 04", to
   TRUCK1, AC8646 by name), the convoy holds at the D hold-short line ("holding short at D").
5. AC8646 lands and rolls past D: "AC8646 lands and rolls past D. The convoy is still
   holding short."

## 3. Procedures proposes, controller approves

1. Right after the STOP (same run) a card appears at the top of the map:
   `procedures agent · proposal · needs controller` —
   **"TRUCK1 cross runway 04 at D after AC8646 passes"**.
2. Click **✓ Approve** (`POST /approve`, `controller_A`). The card goes away; a green
   `✓ TRUCK1 cross runway 04 at D after AC8646 passes · executed · approved_by controller_A`
   card appears in Alerts.
3. Once the jet is past D the convoy crosses: "Controller approved the crossing. Convoy
   crosses after AC8646 passed." (Approve any time after the card appears; the trucks
   never move in front of the jet.)
   - **✕ Reject** instead revokes the pending decision (logged, `controller_revoked`):
     card "✕ Rejected by controller: …"; the trucks stay put.

## 4. Rogue agent: Atlas says no

1. Click **Rogue agent** (any time; no run needed).
2. Alerts panel: **Atlas rejected a "clear" written by an agent**, with both attempts:
   - `insert intents_procedures {action: "clear", ...} -> code 121: Document failed validation`
   - `insert decisions {intent_action: propose_instruction, outcome: executed, approved_by: none} -> code 121`
3. Talking point: the database itself refused it (schema validators); each blocked try is
   stored in `security_events` (insert-only). Nothing reached intents or decisions.

## 5. Mid-run kill: disable Procedures

1. Click **⟲ Reset**. Safety stays **ON**. Click **▶ Play**.
2. After "cleared to land" (23:36:40) and **before** the crossing clearance (23:37:18),
   click the **Procedures** toggle off (logged, `by: controller_A`).
3. The STOP still fires (safety is on). Procedures' next intent, its proposal, comes back
   as a grey `✕ TRUCK1 cross runway 04 at D after AC8646 passes` card
   (`procedures · propose_instruction · rejected`, `rule: agent_disabled`). No Approve card.
4. Click **Procedures** back on afterwards (otherwise it stays off for the next run).

## 6. Audit: "Why was TRUCK1 held?"

Point at the red STOP card from beat 2 (or 5). Each decision card shows the audit trail
straight from the `decisions` document:

- `safety · stop · executed · 23:37:18`
- `rule: tier_autonomous` (why the gate let it execute) and
  `reason: no_crossing_during_active_landing` (the safety rule that fired)
- `authorized_by: <permission _id> · rules normal_v1` (which switch + rule set allowed it)
- `approved_by: — (autonomous STOP)`; on the approved crossing: `approved_by: controller_A`

The Atlas strip at the bottom right is the live change stream (permissions, transmissions,
decisions). The natural-language answer (`GET /audit?q=Why was TRUCK1 held?`) needs
`holdshort.audit.answer` from the audit task; until that branch is merged the endpoint
returns 501 and the cards above are the audit view.

## Checks

```bash
scripts/demo_check.sh       # gate + agents + runner, no API, throwaway <db>_demo_check
scripts/demo_api_check.sh   # demo.sh in throwaway <db>_demo_api, drives the API like the UI (x5 speed)
```

`demo_api_check.sh` uses ports 8000/5173 unless `API_PORT` / `WEB_PORT` are set, and always
stops what it started.

## If something breaks

- Page says `offline · retrying`: the API is down or the tunnel dropped. `curl localhost:8000/health`
  on the VM; `tail logs/demo/api.log`.
- No STOP with Safety ON: is the gate running? `tail logs/demo/gate.log logs/demo/safety.log`.
- `a scenario run is already playing; reset first`: press **⟲ Reset**.
- A switch was left off: `uv run python -m holdshort.gate set-permission procedures on controller_A`
  (or click it in the UI).
- Last resort: `http://localhost:5173/?mock=1` replays the same story in the browser with no backend.
- After the demo: `scripts/demo_stop.sh`.
