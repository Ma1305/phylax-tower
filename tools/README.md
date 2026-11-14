# tools: the smoke feed

`smoke_feed.py` is a small stand-in for the simulator, used by the tests, `scripts/feed.sh` and
`scripts/e2e_check.sh`. Like the simulator it talks to the tower only through `/ingest/*` and
imports nothing from `holdshort/`. For realistic traffic and the LaGuardia replay use
[`simulator/`](../simulator/) instead.

```bash
uv run python tools/smoke_feed.py --api http://localhost:8000 --scenario incident
uv run python tools/smoke_feed.py --scenario normal --speed 2 --no-audio
```

Scenarios: `normal`, `incident`, `intruder`, `both`. Options: `--speed N`, `--loop`,
`--no-audio`, `--no-reset`.

- `fixtures/scenarios.json`: the radio lines, voices and tracks of each scenario.
- `fixtures/audio/`: cache of generated speech (gitignored). With `VOICE_ENABLED=true` and an
  ElevenLabs key the clips are synthetic speech; otherwise they are tones of the right length
  and the tower uses the text sent with each clip.
