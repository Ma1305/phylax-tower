# Hold Short: 60-second demo video

Render project for the submission video. Built with **Remotion 4** (React, headless Chrome,
bundled ffmpeg; no system ffmpeg and no sudo needed). It renders today with placeholders and
renders the final cut with no code changes once the assets are dropped in.

## Render

```bash
cd video
npm ci
npm run render        # -> video/out/preview.mp4   (1920x1080, 30 fps, 59.5 s)
npm run probe         # ffprobe line for the result
```

`npm run render` first runs `scripts/prepare.mjs`, which

1. checks which files from `assets.json` exist in `assets/`, and
2. reads the real repository files listed in `sources.json` and extracts every collection
   name, field name, rule id and code excerpt that appears on screen,

and writes both to `src/generated/data.json` (gitignored). It prints every placeholder that is
still open. Nothing that looks like code is typed into a component.

Other commands: `npm run studio` (interactive preview on port 18080; stop it when done),
`npm run still -- out/f900.png --frame=900` (one frame), `npm run typecheck`.

## Layout

```
script.json    config block (names, repo URL, demo URL) + scenes: id, start_s, end_s, vo_text
assets.json    manifest of expected asset files and what each should contain
sources.json   which repo files / identifiers prepare.mjs reads (edit this if code moves)
assets/        drop assets here (Remotion public dir)
scripts/       prepare.mjs (the pre-render step)
src/           ui.tsx (shared style + building blocks), Root.tsx (timeline, captions),
               scenes/ Incident, Team, What, Catch, Audit, Close (one per scene)
out/           renders (gitignored)
```

## Dropping in assets

Copy the file into `video/assets/` with the exact name below and run `npm run render` again.
A present file replaces its placeholder card; an absent one keeps the card.

| file | scene | what it should contain |
|---|---|---|
| `vo.wav` | all | Full voiceover from 0:00. Absent = silent render on the `script.json` timings. |
| `rec_safety_off.mp4` | 1 incident | Visualizer replaying the reconstruction with Safety OFF. 10 s. Shown desaturated behind the cards. |
| `rec_safety_on.mp4` | 4 catch | The same calls with Safety ON, the stop routed to the owning controller. 16 s. |
| `stop_spoken.wav` | 4 catch | The stop call Hold Short speaks, about 1.5 s. Starts at the SPEAK beat (86 % into scene 4, about 0:35.8). |
| `rec_blocked_write.mp4` | 5 audit | An agent's forbidden write and the resulting `security_events` row. About 4 s. |
| `rec_switch_off.mp4` | 5 audit | A controller switches an agent off; its next intent is refused. About 5 s. |
| `rec_time_travel.mp4` | 5 audit | Visualizer timeline scrubbed back through the event. About 5 s. |
| `team.png` | 2 team | Optional. Real photo or avatars, no generated faces. |

Recordings should be 16:9 and at least as long as their slot (they are cropped to fit, muted,
and start at the slot's first frame). Names, repo URL and demo URL: edit `config` at the top of
`script.json`; `{names}` in the scene 2 `vo_text` is filled from `config.team_names`.

## Retiming (if the voiceover runs long)

All timing lives in `script.json`.

- Change `start_s` / `end_s` of the scenes. Scenes must stay contiguous (each `start_s` = the
  previous `end_s`). The video length is the last `end_s`; keep it at or below **59.5** so the
  container stays under 60 s (the audio track adds about 0.05 s).
- Animations inside a scene are keyed to the scene's progress (0..1), so a longer or shorter
  scene stretches or compresses its beats automatically. No component edits needed.
- Captions are cut from `vo_text` at sentence ends and timed in proportion to their length
  within the scene. To shift a caption, move words between scenes or adjust the scene bounds.
- Beat boundaries inside scenes 4 and 5 are the `BEATS` arrays at the top of
  `src/scenes/Catch.tsx` and `src/scenes/Audit.tsx`, as fractions of the scene, if one beat
  needs more room than the others.

## Where the on-screen code comes from

Read at render time from the working tree this folder sits in (branch `video/scaffold`, cut
from `v2/base`):

| on screen | source file |
|---|---|
| Collection names in Atlas (scene 3) and on chips | `scripts/setup_atlas.py` (`COLLECTIONS`, `INTENT_COLLECTIONS`), `holdshort/contracts.py` |
| Transmission document fields (scene 4b) | `holdshort/contracts.py`, class `Transmission` |
| Intent / decision / security event fields (scenes 4d, 5) | `holdshort/contracts.py`, classes `Intent`, `Decision`, `SecurityEvent` |
| Rules file scroll and highlighted rule (scene 4c) | `holdshort/safety/engine.py` (the `RULE_NO_CROSSING in enabled` branch of `evaluate`) |
| Rule id `no_crossing_during_active_landing` | `holdshort/safety/engine.py`, `scripts/setup_atlas.py` |
| Barrier 1 "Atlas validator": `intents_schema` | `scripts/setup_atlas.py` |
| Barrier 3 "Gate": `_insert_decision`, reason codes `tier_autonomous`, `agent_disabled` | `holdshort/gate/core.py` |
| Intent actions (`stop`, forbidden `clear`), outcomes (`executed`, `rejected`) | `holdshort/contracts.py`, `holdshort/safety/engine.py` |
| Stream ids, lane id, owning controller (`LGA_TWR`, `RWY_04_22`, `ctl_local`) | `docs/v2/world_lga.json` (`holdshort/world/lga.json` is preferred once it exists) |
| The two clearance transcripts (scene 1, reused in 4a/4b) | copied verbatim from `scenarios/laguardia/script.json` events `t02` and `t08` into `video/script.json` |
| Example values in documents (callsigns, error code 121, `intents_procedures action=clear`) | `scenarios/laguardia/script.json`, field descriptions in `holdshort/contracts.py`; listed in `sources.json` |

Only in the v2 spec so far, not yet in code on `v2/base` (shown under their spec names, and
picked up from code automatically once it lands, see below):

| on screen | spec source | expected code |
|---|---|---|
| Barrier 2 "Scoped database": `ScopedDb` | `docs/v2/PLAN.md` | `holdshort/access.py` |
| Collections `state_events`, `context` | `docs/v2/CONTRACTS.md` | `scripts/setup_atlas.py` |
| `state_events` row fields (`kind`, `ref`, `doc`, `by`) | `docs/v2/CONTRACTS.md` | class `StateEvent` in `holdshort/contracts.py` (name is a guess; fix in `sources.json`) |

`v2/base` currently holds the v1 code plus the v2 docs, so a few v1-only names
(`runway_status`, `flight_state`, `close_runway`) exist in code but are deliberately not shown:
the video uses the v2 vocabulary (lane, stream; intents `warn | stop | propose_instruction`).
When the v2 code is merged into `v2/base`, merge or rebase this branch onto it and re-render:
`prepare.mjs` will then read `ScopedDb`, `state_events`, `stream_id`, `lane_id` and
`controller_id` from the code (the samples in `sources.json` already carry those fields; they
appear on screen as soon as the model classes define them). If a file moved, add the new path
to the relevant list in `sources.json`.

Anything not found in code or spec is drawn as a red `[PLACEHOLDER name]` and listed by
`npm run data`.

## Placeholders still open

- Assets: all eight files in the table above (`team.png` is optional).
- `script.json` config: `team_names` (four `[NAME n]`), `demo_url` (`[DEMO URL]`).
  `repo_url` is filled in.
- Spec-only names in the table above (`ScopedDb`, `state_events`, `context`, the
  `state_events` row shape).
- The spoken stop phrase is not shown as text (it is not in code on `v2/base`); scene 4e shows
  only the waveform and the `stop_spoken.wav` slot.

## Content rules followed

1920x1080, 30 fps, 59.5 s. Captions burned in for every `vo_text` line. No crash or fire
imagery, no airline logos, no pilot names, no real ATC audio, no faces. Scene 1 is labelled
RECONSTRUCTION. There is no "clear" intent anywhere except as the forbidden write in scene 5.

## Last verified render

Placeholders only, `assets/` empty:

```
codec_type=video width=1920 height=1080 r_frame_rate=30/1 codec_type=audio r_frame_rate=0/0 duration=59.562667
```
