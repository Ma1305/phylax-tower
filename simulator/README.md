# Phylax Tower simulator

Live-looking traffic, radio and weather for Phylax Tower. It is a separate program:
it talks to the tower only through the `/ingest/*` HTTP API defined in
[`docs/v2/SIMULATOR_CONTRACT.md`](../docs/v2/SIMULATOR_CONTRACT.md), exactly as real feeds
would. It imports nothing from `holdshort/` or `visualizer/` and never touches the database.

What it sends:

- **Positions**: every aircraft, vehicle and person, once a second, in one call.
- **Radio**: one audio clip per call, posted when the call ends, on the Tower, Ground and Ops
  streams at the same time. One stream never carries two calls at once (except the
  deliberate stepped-on call). Each clip carries the true text and speaker for scoring.
- **Weather** reports and **run markers** (so a scenario shows on the visualizer timeline).

What it listens for: `GET /ingest/outbound`, about once a second. Traffic that Phylax Tower
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
| `laguardia` | The 22 March 2026 runway 4 / taxiway D collision, rebuilt from the NTSB preliminary report: Jazz 646 is cleared to land; 117 s later the same controller clears "Truck 1 and company" (seven vehicles) to cross; the jet reaches taxiway D 20 s after that. See below. | Get a stop to Truck 1, by name, within about 13 s of the crossing clearance. Better: warn the controller when the crossing is requested. |
| `intruder` | A person on foot (no callsign, no radio), or with `--intruder vehicle` an unidentified vehicle, is on runway 04 while AC8646 is on final. Nothing is said about it on the radio. | See it in the positions alone: a runway-incursion warning, a stop call for the object on the runway (the vehicle leaves when it hears one addressed to nobody by name), and a go-around if the controller approves one. |
| `degraded_radio` | A wrong readback ("runway one three"), two similar callsigns (AC8646 / AC8464) with the wrong one answering, an unreadable call, a stepped-on transmission. No runway conflict. | Flag the calls (readback mismatch, unclear, confusable); no stop. |
| `low_visibility` | Visibility drops to 3/4 then 1/2 statute mile during an arrival and a departure, then recovers to 5. | The weather agent switches to the low-visibility rule set, then back. |

Callsigns are the ones the tower's callsign list knows (AC8646, AC8464, UA1532, DL2214,
TRUCK1-3, OPS1, ...). Everything spoken is standard phraseology built in `hssim/phrases.py`.

## The LaGuardia reconstruction

`laguardia` replays the accident of 22 March 2026 from the NTSB Aviation Investigation
Preliminary Report DCA26MA161 (public; preliminary and subject to change).

**From the report, exactly:** the jet's distance from taxiway D, altitude and ground speed at
each timestamp (5-mile final at 1,900 ft at 23:35:07; 4,400 ft out at 130 ft at 23:37:04;
touchdown 1,450 ft out at 128 kt at 23:37:17; 90 kt at impact); Truck 1 standing 460 ft from
the runway edge, moving at 23:37:07 as it read back, 29 mph and just over 100 ft from the
runway at 23:37:20; seven vehicles; Truck 7's first call blocked by another transmission; the
controller's stop calls at 23:37:12 and 23:37:20; one controller giving both clearances on the
tower frequency; the 22:51 weather report (7 miles, light rain); the callsigns Jazz 646
(JZA646), Truck 1, Truck 7, Frontier 4195, United 2384.

**Reconstructed:** the wording of the calls (standard phraseology; the first stop call follows
the published recording). **Invented context:** the few calls on the Ground and Ops streams
about the other aircraft's emergency. **One deliberate difference:** the aircraft that had
landed earlier is already off the runway, so the tower is not shown a landing clearance onto
an occupied runway. **Geometry:** the tower's seeded schematic, not a survey; positions are
placed by distance from taxiway D, as the report gives them.

**What the simulator adds:** a stop from Phylax Tower addressed to Truck 1 makes the crew brake
after `--crew-reaction-s` (default 2). The controller's own stop calls are played as they
happened and, as on the night, do not stop the truck. The result reports
`stop_heard_after_s` against `stop_needed_within_s` (about 14.7 s after the clearance with the
default reaction time), and how far from the runway the truck stopped.

```bash
uv run python -m hssim run laguardia --api http://localhost:8000
```

### With a real recording of the frequency

Generated speech can be replaced by a recording you have (it stays outside the repository;
pass its path). The simulator finds each transmission in it, and posts each one at its real
moment with no text attached, so the tower has to transcribe the real audio.

```bash
uv run python -m hssim run laguardia --real-audio ~/Downloads/KLGA-Twr-Mar-23-2026-0330Z.mp3
```

The start time is read from a name ending in `-0330Z` (03:30 UTC is 23:30 local); otherwise
give `--real-audio-start 03:30:00`, or for a short excerpt `--real-audio-clearance-at MM:SS`
(where in the file the controller says "Truck 1 and company, cross runway 4").
`--real-audio-shift S` nudges the alignment. Only the stretch from 12 s before the landing
clearance to 6 s after the second stop call is used. Anything but WAV needs ffmpeg.

## Speech

`--tts auto` picks ElevenLabs when `ELEVENLABS_API_KEY` is set and `VOICE_ENABLED` is true (a
different voice per speaker), else macOS `say`, else plain tones. Clips get a radio effect
(band-pass, compression, noise) with ffmpeg when it is installed, and are cached in
`simulator/.cache/audio/` so each line is generated once. Before a run the simulator
pre-generates every line of the scenario; `python -m hssim warm all` does that for all of them.

Keys are read from the environment or a `.env` (`$SIM_ENV`, `./.env`, the repo's `.env`) and
are never printed.

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
  realaudio.py      finds and cuts the transmissions in a real recording
  stub_tower.py     a stand-in tower that implements /ingest/* for development and tests
  scenarios/        blocks.py (arrival, departure, crossing, apron traffic) + one file each
tests/              every scenario played fast against the stand-in tower
```

## Tests

```bash
uv run pytest -q
```
