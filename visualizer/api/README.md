# Phylax Tower visualizer API

Read-only API for the visualizer (`/viz/*` in `docs/v2/CONTRACTS.md`). It is a separate system
from the tower: its own `pyproject.toml`, no imports from `holdshort/`, and it never writes to
the tower's database. Controller actions go to the tower's `/control/*` API, not here.

## Run

```bash
cd visualizer/api
uv sync
uv run uvicorn app:app --port 8100          # or: PORT=8100 uv run python app.py  (honours $PORT)
```

Settings (environment, or `~/plane-management-2/.env`, or a local `.env`; see `.env.example`):

| Variable | Meaning |
|---|---|
| `VIZ_MONGODB_URI` | connection string for the visualizer (a read-only Atlas user is enough); falls back to `MONGODB_URI` |
| `MONGODB_DB` | database, default `holdshort_v2` |
| `CORS_ORIGINS` | extra allowed origins, comma separated. Any `localhost` / `127.0.0.1` port is always allowed |
| `PORT` | port for `python app.py` (default 8100) |

## Routes

| Route | Returns |
|---|---|
| `GET /viz/health` | `{ok, db, live, clients}` (`live` = the change stream is open) |
| `GET /viz/world?tower_id=LGA[&controller_id=]` | `{tower, lanes[], hold_points[], controllers[], streams[]}` |
| `GET /viz/bounds?tower_id=LGA` | `{from, to}` earliest and latest stored `t` |
| `GET /viz/snapshot?tower_id=LGA[&t=ISO][&controller_id=]` | the picture now, or rebuilt at `t` |
| `GET /viz/tracks?tower_id=LGA&from=ISO&to=ISO[&step_s=1]` | `{objects: {id: {kind, callsign, points: [[t, lon, lat, alt_ft, heading]]}}}` |
| `GET /viz/timeline?tower_id=LGA&from=ISO&to=ISO` | `{marks: [{t, type, stream_id, severity, label, id}]}` |
| `GET /viz/streams/{stream_id}/clips?from=ISO&to=ISO` | `{clips: [{clip_id, t_start, t_end, duration_s, direction, has_audio, transmission}]}` |
| `GET /viz/audio/{clip_id}` | audio bytes from GridFS bucket `audio`; `Range` requests answer 206 |
| `WS /viz/live?tower_id=LGA` | `{type: "snapshot", doc}` first, then one `{type, doc}` per change |

Notes for the web app:

- `from` / `to` default to the last 15 minutes. Times are ISO 8601 with `Z`; ids are strings.
- Every decision (in `decisions`, `pending` and live `decision` messages) carries its intent's
  fields as `intent` (`action`, `severity`, `target`, `reason`, `proposal_text`, `detail`, ...).
- `/viz/snapshot` also carries `agent_state[]`: `{_id: process name, identity, updated_at}` for
  every tower process that recorded which Atlas identity it connected with (`generated` user,
  `static` connection string or `shared` connection; never a password or URI). It is the current
  value in history mode too.
- `controller_id` only adds `"mine": true/false` (lanes and streams in `/viz/world`; lane
  status, pending items and transmissions in `/viz/snapshot`). Nothing is hidden.
- Live `object` messages are coalesced: at most two per second per object, newest state wins.
  `object_removed` carries `{_id}`. `clip` messages have the same shape as the clips route
  (plus `stream_id`, `status`).
- `/viz/tracks` keeps at most six hours and 3600 points per object: the answer's `step_s` says
  which step was really used.

## Time travel

Rebuilt from stored records only, so a reviewer can reconstruct what happened afterwards:

- objects at `t` = the last `positions` document per object in `(t-30s, t]`, minus objects with
  a later `object_removed` state event (`label`, `clearance`, `hold_short` are not in the time
  series, so they come back as `null` / `false`);
- lane status and permissions = the last `state_events` document per `ref` up to `t`
  (index `(tower_id, kind, ref, t)`);
- pending = `pending_approval` decisions with no later decision for the same intent up to `t`;
- decisions (last 50), transmissions (last 100) and weather = the insert-only logs up to `t`.

## Demo data

```bash
uv run python seed_demo.py --db holdshort_v2_demo     # refuses holdshort and holdshort_v2
MONGODB_DB=holdshort_v2_demo uv run uvicorn app:app --port 18100
```

Writes a three-minute history ending now: the world from `docs/v2/world_lga.json` (lane `area`
polygons built from centerline + width), six moving objects (two aircraft, two vehicles, a
person, an unidentified target), ten clips on three streams with generated tones as audio, lane
status changes with their `state_events`, a weather advisory and every decision outcome: an
autonomous safety stop that is in effect (`tier_autonomous`, spoken at once as a `HOLDSHORT`
outbound call), an earlier one a controller lifted (`controller_revoked`), a proposal the gate
refused (`mode_reactive`), a stop from a `needs_approval` agent that waited and was rejected
(`controller_rejected`) and a proposal that is still pending. The collections
it writes are emptied first. It creates no TTL index, so the six objects stay for the web app.

## Tests

```bash
uv run pytest        # seeds holdshort_v2_t<task number>, drops it at the end
uv run ruff check . && uv run ruff format --check .
```
