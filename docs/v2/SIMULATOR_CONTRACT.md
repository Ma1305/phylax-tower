# Phylax Tower v2: simulator contract (the `/ingest/*` API)

For the session building the simulator. This file is the only definition of how data enters
the tower. The tower implements it; the simulator calls it. Read `PLAN.md` for the big picture
and `world_lga.json` for the airport.

## Rules for the simulator

- It lives in `simulator/` at the repo root, on its own branch cut from `v2/base`. It must not
  import from `holdshort/` or `visualizer/` and must not touch the database: HTTP only.
- It behaves like live feeds: every `t` is the real UTC time the thing happens. To run a
  scenario faster, shorten the gaps; never send old or future timestamps (the tower rejects a
  `t` more than 120 s away from its own clock).
- Tower base URL from `TOWER_API` (default `http://localhost:8000`). If `INGEST_KEY` is set on
  the tower, send it as header `X-Ingest-Key`.
- JSON bodies, UTF-8. Times are ISO 8601 UTC with `Z` (`2026-11-14T19:05:12.250Z`).
  Coordinates are decimal degrees.
- No real ATC recordings, airline logos or crew names. Generate speech (ElevenLabs TTS, a
  distinct voice per speaker) and degrade it to sound like radio.

## Endpoints

### `GET /ingest/world?tower_id=LGA`
Returns `{tower, lanes[], hold_points[], controllers[], streams[], points{}}`: the same content
as `world_lga.json`, with each lane's `area` polygon. Use it for geometry and ids.

### `POST /ingest/positions`
One call carries the current position of any number of objects (recommended: all objects, once
per second).
```json
{
  "tower_id": "LGA",
  "t": "2026-11-14T19:05:12Z",
  "objects": [
    {"object_id": "AC8646", "kind": "aircraft", "callsign": "AC8646", "label": "A320",
     "status": "airborne", "lat": 40.7401, "lon": -73.9132,
     "alt_ft": 1200, "heading_deg": 44, "speed_kt": 135},
    {"object_id": "TRUCK1", "kind": "vehicle", "callsign": "TRUCK1", "label": "Fire truck",
     "status": "ground", "lat": 40.7750, "lon": -73.8775, "heading_deg": 314, "speed_kt": 0},
    {"object_id": "OBJ-17", "kind": "person", "callsign": null, "label": "Person on foot",
     "status": "ground", "lat": 40.7741, "lon": -73.8770}
  ]
}
```
- `object_id` is stable for the life of the object. Use the callsign when there is one.
- `kind`: `aircraft | vehicle | person | unknown`. `status`: `airborne | ground`.
- Required per object: `object_id`, `kind`, `status`, `lat`, `lon`. `t` defaults to now.
- Up to 500 objects per call.
- Response `200 {"accepted": 3, "lanes": {"AC8646": null, "TRUCK1": "TWY_D", "OBJ-17": null}}`
  (the lane each object is on).
- An object that stops being reported disappears after 120 s. To remove it at once:

### `POST /ingest/objects/{object_id}/remove`
`{"tower_id": "LGA", "reason": "parked at gate"}` -> `200 {"removed": true}`

### `POST /ingest/audio` (multipart form)
One request per utterance (one push-to-talk burst), sent when the utterance **ends**.
Send utterances on different streams at the same time; the tower processes them concurrently.
- part `meta`: a JSON string
  ```json
  {"tower_id": "LGA", "stream_id": "LGA_TWR",
   "t_start": "2026-11-14T19:05:14.200Z", "duration_s": 4.6,
   "speaker": "TOWER",
   "truth_text": "Air Canada eight six four six, runway four, cleared to land."}
  ```
- part `file`: the audio (`audio/wav`, `audio/mpeg` or `audio/ogg`; at most 60 s and 5 MB).
- `stream_id` must exist (see the world, or create one below). `speaker` and `truth_text` are
  optional ground truth: the tower stores them for scoring and falls back to `truth_text` when
  speech-to-text is off or fails. Do not rely on the tower using `speaker`.
- Response `202 {"clip_id": "..."}`. Transcription is asynchronous.

### `POST /ingest/transcripts`
The same utterance without audio (for fast tests, or streams with no sound):
`{"tower_id", "stream_id", "t_start", "duration_s"?, "speaker"?, "text", "confidence"?}`
-> `202 {"clip_id": "..."}`. The visualizer shows it with no playable audio.

### `POST /ingest/weather`
`{"tower_id": "LGA", "t"?: "...", "visibility_sm": 0.75, "wind": "03012KT", "raw"?: "..."}`
-> `200`. Visibility under 1 sm makes the weather agent ask for the low-visibility rule set.

### `POST /ingest/streams`
Create or update a radio stream:
`{"id": "LGA_APP", "tower_id": "LGA", "name": "Approach 120.8", "kind": "approach",
"frequency_mhz": 120.8, "controller_id": "ctl_local", "station": "APPROACH"}` -> `200`.
The three seeded streams are `LGA_TWR`, `LGA_GND`, `LGA_OPS`.

### `POST /ingest/runs` and `POST /ingest/runs/{run_id}/end`
Mark the start and end of a scenario so it shows on the visualizer's timeline:
`{"tower_id": "LGA", "scenario": "laguardia_crossing", "kind": "incident", "note": "..."}`
-> `200 {"run_id": "..."}`. `kind`: `normal | incident`.

### `POST /ingest/reset`
`{"tower_id": "LGA", "scope": "live"}` clears live state only (objects, lane status back to
clear, working memory). The permanent log is never deleted. Call it before a scenario.

### `GET /ingest/outbound?tower_id=LGA&since=ISO`
What Phylax Tower has told traffic, so simulated traffic can obey. Poll about once a second.
```json
{"now": "2026-11-14T19:06:02Z",
 "calls": [
   {"t": "2026-11-14T19:05:58Z", "decision_id": "...", "stream_id": "LGA_GND",
    "action": "stop", "target": ["TRUCK1"], "lane_id": "RWY_04_22",
    "text": "Truck one, STOP. Hold short runway zero four.",
    "approved_by": "ctl_local", "clip_id": "..."}
 ]}
```
`action` is `stop` or `propose_instruction` (an instruction a controller approved; read
`text`). A target that receives `stop` should stop moving. Pass the returned `now` as the next
`since`.

### Errors
`400` bad body (the response says which field), `401` wrong or missing `X-Ingest-Key`,
`404` unknown `tower_id` / `stream_id` / `object_id`, `413` audio too large,
`422` timestamp too far from now.

## What the tower does with it (so you know what to expect)

1. Positions update `objects` and the position history; the tower works out which lane each
   object is on and each lane's state (clear, reserved, occupied, conflict, closed).
2. Each clip is transcribed and parsed into a transmission (who, to whom, instruction, runway,
   taxiway, readback check). Speak standard phraseology and use the callsigns of the objects
   you report, spoken the usual way ("Air Canada eight six four six", "Truck one").
   Runways are spoken as digits ("runway four" = 04), taxiways by letter name ("Delta" = D).
3. Agents warn the owning controller, or propose a call (a stop, or an instruction) that the
   controller approves in the visualizer. Approved calls appear in `/ingest/outbound`.

## Scenarios we need from the simulator

- **Normal traffic** (the long-running default): several arrivals on runway 04 a few minutes
  apart, departures, taxiing aircraft on B, vehicles on the apron and crossing at D or G with
  proper requests, clearances and readbacks spread over the Tower, Ground and Ops streams.
  It must produce no stops: a correct system stays quiet.
- **Incident: runway crossing during a landing** (LaGuardia-style): an aircraft is cleared to
  land on 04 on Tower while Ground clears a fire truck to cross 04 at D. About 25 s from the
  crossing clearance to the conflict. With the tower's agents on and the stop approved, the
  truck holds short; otherwise it crosses.
- **Incident: uncleared object** on a runway (a vehicle or a person with no clearance and no
  radio call) while an aircraft is on final.
- **Degraded radio**: an unclear call, a wrong readback, two similar callsigns on one stream
  (AC8646 and AC8464), a stepped-on transmission.
- **Low visibility**: weather drops under 1 sm during normal traffic.

Each scenario should be startable by name from the command line, announce itself with
`/ingest/runs`, and be repeatable.

## Until the tower is up

Develop against a stub that accepts these calls, or against the real tower on the VM once the
`ingest` task has landed (`http://localhost:8000`, see `TESTING.md` on the final branch).
`tools/smoke_feed.py` on the tower side is a tiny stand-in used only for our own tests; the
simulator replaces it.
