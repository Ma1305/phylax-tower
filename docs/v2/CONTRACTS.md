# Hold Short v2: contracts

The single source of truth for collection shapes, enums and the `/control/*` and `/viz/*`
APIs. `/ingest/*` is defined in `SIMULATOR_CONTRACT.md`. `holdshort/contracts.py` (Pydantic)
and the validators in `scripts/setup_atlas.py` must match this file exactly.

Conventions:
- Database `holdshort_v2`. Tower id `LGA`. All ids below are strings unless marked ObjectId.
- Timestamps: timezone-aware UTC, stored as BSON dates, wall-clock. JSON: ISO 8601 with `Z`.
- Positions: GeoJSON `{"type": "Point", "coordinates": [lon, lat]}`.
- Callsigns: `^[A-Z0-9]+$` (AC8646, TRUCK1). Controller stations on the radio: `TOWER`,
  `GROUND`, `OPS` (from `streams.station`). Unknown speaker: `UNKNOWN`. Hold Short itself:
  `HOLDSHORT`.
- "insert-only" = never updated or deleted by anyone.

## Enums

```
LaneKind          runway | taxiway | apron
LaneState         clear | reserved | occupied | conflict | closed
ObjectKind        aircraft | vehicle | person | unknown
ObjectStatus      airborne | ground
StreamKind        tower | ground | ops | approach | other
ControllerRole    local | ground | supervisor
TransmissionKind  clearance | request | readback | report | stop | other
Instruction       land | takeoff | cross | hold_short | line_up_wait | taxi | go_around | none
Readback          matched | mismatch | missing | not_applicable
AgentName         listener | safety | procedures | weather
PermissionAgent   listener | safety | procedures | weather | system     (system = master switch)
AgentMode         reactive | proactive
PermissionTier    autonomous | needs_approval | never
IntentAction      warn | stop | propose_instruction | close_lane | reopen_lane | set_rule_set
                  (never "clear")
Severity          info | caution | warning | critical
DecisionOutcome   executed | rejected | pending_approval | revoked
ClipStatus        queued | processing | done | failed
Direction         in | out
```

## World (seeded from `world_lga.json`; changes are rare and logged in `state_events`)

**towers** `{_id, name, icao, center: Point}`

**lanes** `{_id, tower_id, kind: LaneKind, name, designators: [str], width_m: number|null,
centerline: LineString|null, area: Polygon, crosses: [lane_id]}`. Index: `2dsphere` on `area`.
`area` is built from centerline + width for runways and taxiways.

**hold_points** `{_id, tower_id, lane_id, protects: lane_id, position: Point}`

**controllers** `{_id, tower_id, name, role: ControllerRole, lanes: [lane_id],
streams: [stream_id], active: bool}`. A lane has at most one owner. A supervisor owns no lane
and may act on any.

**streams** `{_id, tower_id, name, kind: StreamKind, frequency_mhz: number|null,
controller_id, station}`

Helpers every module uses (in `holdshort/world/`): `lane_for_designator(tower_id, "04")`
-> lane id, `lane_at(point)` -> lane id or None, `owner_of(lane_id)` -> controller id (the
supervisor's id when no one owns it), `stream_of(controller_id)`.

## Live state (overwritten in place)

**objects** (one per tracked thing; TTL on `expires_at`)
```
_id            object id (the callsign when it has one, else the feed's id, e.g. "OBJ-17")
tower_id
kind           ObjectKind
callsign       str|null
label          str|null            free text for the map ("Fire truck", "Person on foot")
status         ObjectStatus
position       Point
alt_ft, heading_deg, speed_kt      number|null
lane_id        lane the position is inside, or null      (set by ingest)
clearance      {instruction, lane_id, at, since, transmission_id} | null   (set by state)
hold_short     bool                                                         (set by state)
updated_at
expires_at     updated_at + OBJECT_TTL_S (120 s)
```
An object counts as *on* a runway when its position is inside the runway `area` and it is
`ground`, or `airborne` below 200 ft.

**lane_status** (one per lane)
```
_id            lane id
tower_id, controller_id (owner)
state          LaneState
occupants      [object_id]
reservations   [{object_id|null, callsign, instruction, since, transmission_id}]
reason         str|null            why it is in conflict / closed
updated_at
```
State priority: `closed` > `conflict` > `occupied` > `reserved` > `clear`.
`reserved` = an active land / takeoff / cross / line_up_wait clearance exists.
`occupied` = at least one occupant. `conflict` = a land or takeoff reservation together with
any other occupant or any other reservation on the same lane.

**context** (the agents' working memory; TTL on `expires_at`)
```
_id            ObjectId
tower_id
kind           clearance | request | report | alert | weather | note
summary        one line, e.g. "18:41:07 TWR TOWER->AC8646 cleared to land RWY 04 (conf 0.95)"
callsigns      [str]
lane_ids       [lane_id]
stream_id      str|null
instruction    Instruction|null    copied from the transmission
readback       Readback|null       copied from the transmission
flag           str|null            copied from the transmission ("unclear - say again", ...)
source         {coll, id}
t
superseded_by  ObjectId|null
expires_at
```
Lifetimes: clearance 15 min, or 60 s after it is superseded (a newer instruction to the same
callsign, or its object is gone); request / report 5 min; alert 10 min; other / unclear 2 min;
weather: only the newest stays, the older one is superseded. Agents read `context`,
`objects`, `lane_status` and the world. They do not query `transmissions`.

**permissions** (one per PermissionAgent)
```
_id = agent, agent, enabled: bool, mode: AgentMode, tier: PermissionTier,
read: [collection], write: [collection], by, changed_at
```
Seed: safety proactive / autonomous (a critical stop runs at once, is spoken, logged and shown
to the owning controller, who can lift it); procedures proactive / needs_approval; weather
proactive / autonomous; listener reactive / autonomous; system (master) enabled.
Seeded access lists:

| agent | read | write |
|---|---|---|
| listener | audio_clips, streams, controllers, lanes, objects, callsigns, audio.files, audio.chunks (GridFS `audio`) | transmissions, audio_clips |
| safety | context, objects, lane_status, lanes, controllers, streams, rules, permissions, weather | intents_safety |
| procedures | context, objects, lane_status, lanes, controllers, streams, rules, decisions, intents_safety, incidents, permissions | intents_procedures |
| weather | weather, rules | intents_weather |

**rules** as v1 (`version`, `name`, `active`, `rules[]`). Rule ids:
`no_crossing_during_active_landing`, `no_landing_on_occupied_runway`, `runway_incursion`
(an object is on a runway that another object is cleared to land on or take off from, and has
no clearance for it), `uncleared_entry` (an object is on a runway with no clearance at all),
`readback_mismatch`, `no_ai_clearances`, and in `low_vis_v1` also `low_vis_one_movement`.

**agent_state** `{_id: process name, resume_token, updated_at}`

## Logs (insert-only unless noted)

**positions**: time-series collection (`timeField: "t"`, `metaField: "meta"`,
`expireAfterSeconds`: 86400).
`{t, meta: {object_id, tower_id, kind, callsign}, position: Point, alt_ft, heading_deg,
speed_kt, status, lane_id}`

**state_events**: every change to current state that the timeline must be able to rebuild.
`{_id, t, tower_id, kind: "lane_status"|"permission"|"controller"|"rule_set"|"object_removed",
ref: id of the changed thing, doc: full document after the change, by: str|null}`.
Index `(tower_id, kind, ref, t)`. Whoever overwrites `lane_status`, `permissions`,
`controllers` or the active rule set also inserts the matching `state_events` document.

**audio_clips** (the queue and index of clips; `status`, `transmission_id`, `error` are
updated, nothing else)
```
_id ObjectId, tower_id, stream_id, direction: Direction,
t_start, t_end, duration_s,
audio_id        GridFS file id in bucket "audio" | null (text-only utterance)
mime            str|null
truth_text      str|null    what was really said, if the feed knows it
speaker         callsign|station|null   hint from the feed; never required
status          ClipStatus
transmission_id ObjectId|null
error           str|null
created_at
```
Audio bytes live in GridFS bucket `audio`.

**transmissions**
```
_id ObjectId, tower_id, stream_id, controller_id (owner of the stream),
t (= clip t_start), t_end, direction: Direction,
from, to                 callsign or station
kind: TransmissionKind, instruction: Instruction,
runway: "04"|null, at: "D"|null, lane_id: lane the instruction is about | null,
transcript, confidence 0..1, readback: Readback,
verified: bool           false whenever confidence < 0.8
flag: str|null           e.g. "unclear - say again", "no callsign", "confusable: AC8464"
clip_id, audio_id        ObjectId|null
synthetic: bool          true when Hold Short spoke it
decision_id: ObjectId|null   the decision that produced a synthetic call
```

**intents_safety / intents_procedures / intents_weather**
```
_id ObjectId, t, tower_id, agent (must equal the collection's agent),
action: IntentAction, severity: Severity,
target: [callsign], object_ids: [object_id],
lane_id|null, runway|null, stream_id|null (where to speak),
reason (rule id or short code), conflicts_with: str|null,
proposal_text: str|null   exact words to transmit, or the rule set version for set_rule_set
detail: str|null          one or two sentences for the controller
```

**decisions**
```
_id ObjectId, t, tower_id, intent_id, agent, intent_action, severity,
outcome: DecisionOutcome, rule (reason code),
lane_id|null, controller_id (who it is routed to), stream_id|null,
text: str|null            what is shown, and spoken if executed
target: [callsign], mode: AgentMode (agent's mode at decision time),
authorized_by: {permission_id, rule_set_version},
approved_by: controller_id|null, revokes: ObjectId|null, notified: [str]
```
Validator: `propose_instruction` + `executed` requires a non-empty `approved_by`.

Gate reason codes: `agent_disabled`, `tier_never`, `no_permission`, `no_active_rule_set`,
`mode_reactive` (a reactive agent asked for anything but `warn`), `advisory` (a `warn`:
executed, shown, never spoken), `tier_autonomous` (a `stop` run without a controller),
`needs_approval`, `controller_approved`, `controller_rejected` (outcome `revoked`,
`revokes` = the pending decision), `controller_revoked`, `not_lane_owner` (403 on the API,
plus a `security_events` document).

Decision table: disabled -> rejected. `warn` -> executed/`advisory`. Reactive mode and not
`warn` -> rejected/`mode_reactive`. `stop` -> executed if tier autonomous, else
pending_approval. `propose_instruction` -> always pending_approval. `close_lane`,
`reopen_lane`, `set_rule_set` -> executed if autonomous, else pending_approval.
`controller_id` = `owner_of(lane_id)`; `stream_id` = the intent's, else the owner's stream.

**security_events** `{_id, t, agent|controller_id, kind: "validator_rejected"|"read_denied"|
"write_denied"|"not_lane_owner", attempted, error_code: int|null, error}`

**weather** `{_id, t, tower_id, visibility_sm, wind, raw: str|null}`

**sim_runs** (written through `/ingest/runs`; markers on the timeline)
`{_id ObjectId, tower_id, scenario, kind: "normal"|"incident", note, started_at, ended_at|null}`

Kept from v1 unchanged: `callsigns`, `incidents` (Atlas Search / Vector Search).

## Control API (tower, port 8000)

Controller identity is the `controller_id` in the request body (no login in this build).

```
GET  /health
GET  /control/controllers?tower_id=LGA
GET  /control/pending?controller_id=ctl_local        pending decisions routed to them
                                                     (a supervisor sees all)
POST /control/approve   {decision_id, controller_id}
POST /control/reject    {decision_id, controller_id, reason?}
POST /control/revoke    {decision_id, controller_id}
POST /control/permissions/{agent}  {by: controller_id, enabled?, mode?, tier?}
GET  /control/decisions?tower_id=LGA[&controller_id=][&since=ISO][&limit=100]
                                                     the decision log, newest first, each
                                                     with its intent merged in as `intent`
GET  /control/audit?q=...                            the audit agent's answer (as v1)
```
Errors: 400 bad input, 403 `not_lane_owner`, 404 unknown id, 409 already decided.
CORS: allow any `localhost` / `127.0.0.1` origin and the origins in `CORS_ORIGINS`.

## Visualizer API (port 8100, read-only; reads Atlas directly with `VIZ_MONGODB_URI`, falling
back to `MONGODB_URI`; database from `MONGODB_DB`, default `holdshort_v2`)

```
GET /viz/health
GET /viz/world?tower_id=LGA
      -> {tower, lanes[], hold_points[], controllers[], streams[]}
GET /viz/bounds?tower_id=LGA
      -> {from, to}     earliest and latest t we hold
GET /viz/snapshot?tower_id=LGA[&t=ISO][&controller_id=...]
      -> {t, live: bool, objects[], lane_status[], permissions[], weather|null,
          pending[], decisions[] (last 50 up to t), transmissions[] (last 100 up to t)}
      Without t: the current collections. With t: objects = the last position of each object
      in (t-30s, t]; lane_status and permissions = the last state_events document per ref up
      to t; pending = pending_approval decisions with no later decision for the same intent
      up to t.
GET /viz/tracks?tower_id=LGA&from=ISO&to=ISO[&step_s=1]
      -> {objects: {object_id: {kind, callsign, points: [[t, lon, lat, alt_ft, heading]]}}}
GET /viz/timeline?tower_id=LGA&from=ISO&to=ISO
      -> {marks: [{t, type: "transmission"|"decision"|"alert"|"run"|"permission",
                   stream_id|null, severity|null, label}]}
GET /viz/streams/{stream_id}/clips?from=ISO&to=ISO
      -> {clips: [{clip_id, t_start, t_end, duration_s, direction, has_audio,
                   transmission: {...}|null}]}
GET /viz/audio/{clip_id}          audio bytes (correct Content-Type, Range supported)
WS  /viz/live?tower_id=LGA
      first {type: "snapshot", doc: <snapshot>}, then one message per change:
      {type: "object"|"object_removed"|"lane_status"|"transmission"|"clip"|"decision"|
             "permission"|"weather", doc}
```
`controller_id` on `/viz/snapshot` only adds `"mine": true` on lanes, streams and pending
items that belong to that controller; it never hides data. Filtering is done in the web app.
