# Hold Short simulator

Live-looking traffic, radio and weather for the Hold Short tower. It is a separate program:
it talks to the tower only through the `/ingest/*` HTTP API defined in
[`docs/v2/SIMULATOR_CONTRACT.md`](../docs/v2/SIMULATOR_CONTRACT.md), exactly as real feeds
would. It imports nothing from `holdshort/` or `visualizer/` and never touches the database.

What it sends:

- **Positions**: every aircraft, vehicle and person, once a second, in one call.
- **Radio**: one audio clip per call, posted when the call ends, on the Tower, Ground and Ops
  streams at the same time. One stream never carries two calls at once (except the
  deliberate stepped-on call). Each clip carries the true text and speaker for scoring.
- **Weather** reports and **run markers** (so a scenario shows on the visualizer timeline).

What it listens for: `GET /ingest/outbound`, about once a second. Traffic that Hold Short
tells to stop, stops; an aircraft told to go around, goes around.

Every timestamp is the real current UTC time. `--speed` shortens the waits only.

## Run

```bash
cd simulator
uv sync                      # or: python -m venv .venv && .venv/bin/pip install -e . pytest
uv run python -m hssim list
uv run python -m hssim run crossing --api http://localhost:8000
```

No tower yet? Use the built-in stand-in tower:

```bash
uv run python -m hssim run crossing --stub --no-audio --speed 20              # nobody helps
uv run python -m hssim run crossing --stub --auto-stop 6 --no-audio --speed 20 # a stop arrives
uv run python -m hssim stub --port 8000 --auto-stop 6    # or serve it for other tools
```

Useful options (see `python -m hssim run --help`): `--speed N`, `--no-audio` (text only, via
`/ingest/transcripts`), `--tts auto|elevenlabs|say|tone`, `--radio clean|mild|medium|brutal`,
`--no-reset`, `--short` (end when the runway part is over), `--lead-s 25` (crossing),
`--intruder person|vehicle`, `--cycles N` / `--loop` (normal).

The exit code is 0 when the scenario ended safely and 3 when it ended in the conflict it is
built to cause (the crossing with no stop, the intruder with a landing).

### Continuous traffic with incidents on demand

```bash
uv run python -m hssim serve --api http://localhost:8000      # normal traffic forever
uv run python -m hssim inject crossing                        # from another terminal
curl -s localhost:8200/status
```

An injected incident starts as soon as the traffic already in progress has finished, so the
runway is quiet when it begins. Injecting needs a token: `serve` creates one at start-up
(`.cache/control-token`, readable only by you) and `inject` on the same machine reads it, so
a web page open in a browser cannot trigger an incident. Set `SIM_CONTROL_TOKEN` on both
sides to choose your own.

## Scenarios

| Name | What happens | What a working tower should do |
|---|---|---|
| `normal` | An arrival, a departure and a vehicle crossing at D per cycle, each with request, clearance and readback; an ops vehicle on the apron; weather. The runway is given to one movement at a time. | Stay quiet: no stop. |
| `crossing` | AC8646 is cleared to land on 04 by Tower. About 25 s before it passes D, Ground clears fire truck TRUCK1 to cross 04 at D. The truck reads back and rolls. | Warn the tower controller and get a stop to TRUCK1 before it enters the runway. The truck then holds short, and crosses later if a controller approves an instruction to cross. Without a stop it crosses in front of the landing aircraft. |
| `intruder` | A person on foot (no callsign, no radio), or with `--intruder vehicle` an unidentified vehicle, is on runway 04 while AC8646 is on final. Nothing is said about it on the radio. | See it in the positions alone: a runway-incursion warning, a stop call for the object on the runway (the vehicle leaves when it hears one addressed to nobody by name), and a go-around if the controller approves one. |
| `degraded_radio` | A wrong readback ("runway one three"), two similar callsigns (AC8646 / AC8464) with the wrong one answering, an unreadable call, a stepped-on transmission. No runway conflict. | Flag the calls (readback mismatch, unclear, confusable); no stop. |
| `low_visibility` | Visibility drops to 3/4 then 1/2 statute mile during an arrival and a departure, then recovers to 5. | The weather agent switches to the low-visibility rule set, then back. |

Callsigns are the ones the tower's callsign list knows (AC8646, AC8464, UA1532, DL2214,
TRUCK1-3, OPS1, ...). Everything spoken is standard phraseology built in `hssim/phrases.py`.

## Speech

`--tts auto` picks ElevenLabs when `ELEVENLABS_API_KEY` is set and `VOICE_ENABLED` is true (a
different voice per speaker), else macOS `say`, else plain tones. Clips get a radio effect
(band-pass, compression, noise) with ffmpeg when it is installed, and are cached in
`simulator/.cache/audio/` so each line is generated once. Before a run the simulator
pre-generates every line of the scenario; `python -m hssim warm all` does that for all of them.

Keys are read from the environment or a `.env` (`$SIM_ENV`, `./.env`, the repo's `.env`,
`~/plane-management-2/.env`) and are never printed.

## Layout

```
hssim/
  cli.py            command line
  client.py         the only code that talks to the tower (/ingest/*)
  engine.py         clock, 1 Hz position reports, one-speaker-per-stream radio, outbound poll
  actors.py         aircraft / vehicles / people following legs
  world.py geo.py   the airport and its geometry (from GET /ingest/world)
  phrases.py        spoken callsigns, runways, taxiways
  audio.py          speech engines, radio effect, cache
  stub_tower.py     a stand-in tower that implements /ingest/* for development and tests
  scenarios/        blocks.py (arrival, departure, crossing, apron traffic) + one file each
tests/              every scenario played fast against the stand-in tower
```

## Tests

```bash
uv run pytest -q
```
