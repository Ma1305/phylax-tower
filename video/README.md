# Phylax Tower: 60-second demo video

The render project for the submission video, built with Remotion 4 (React, headless Chrome and
a bundled ffmpeg; no system ffmpeg needed). 1920x1080, 30 fps, 59.5 s, captions burned in.

## Render

```bash
cd video
npm ci
npm run render        # -> out/preview.mp4
npm run probe         # one ffprobe line for the result
```

`npm run render` first runs `scripts/prepare.mjs`, which checks which files from `assets.json`
are present in `assets/` and reads the real repository files listed in `sources.json`
(`scripts/setup_atlas.py`, `holdshort/contracts.py`, `holdshort/safety/engine.py`,
`holdshort/gate/core.py`, `holdshort/access.py`, `docs/v2/CONTRACTS.md`). Every collection name,
field name, rule id and code excerpt on screen comes from those files; nothing that looks like
code is typed into a component. Anything it cannot find is drawn as a red `[PLACEHOLDER name]`.

Other commands: `npm run studio` (interactive preview on port 18080),
`npm run still -- out/f900.png --frame=900` (one frame), `npm run typecheck`.

## Files

```
script.json    product name, team names and the scenes: id, start_s, end_s, vo_text
VOICEOVER.md   the voiceover script, one block per scene
assets.json    the asset files each scene expects and what each should contain
sources.json   which repository files and identifiers prepare.mjs reads
assets/        the assets (Remotion public directory)
scripts/       prepare.mjs, the step before every render
src/           ui.tsx (shared style), Root.tsx (timeline, captions), scenes/ (one file per scene)
out/           renders (gitignored)
```

Scenes: Incident, Team, What, Catch, Audit, Close.

## Assets

In the repository: `vo.wav` (the voiceover) and the two NTSB images of scene 1. Not in the
repository: the screen recordings (`rec_*.mp4`), the spoken stop (`stop_spoken.wav`) and the
optional camera clips (`cam_*.mp4`). A slot whose file is missing renders as a labelled
placeholder; copy the file into `assets/` under the name in `assets.json` and render again.
Recordings should be 16:9 and at least as long as their slot.

Scene 1 uses real material from the NTSB investigation
[DCA26MA161](https://www.ntsb.gov/investigations/pages/DCA26MA161.aspx): the investigation photo
(`incident_photo.jpg`) and figure 2 of the preliminary report (`incident_map.png`). The headline
facts and the two clearance times are from that report; the wording of the clearances is a
reconstruction and is labelled as such on screen. NTSB material is a US government work and is
credited on screen. This was a fatal accident: no crew names appear, and no real ATC audio is
used anywhere in the video.

## Retiming

All timing lives in `script.json`. Scenes must stay contiguous (each `start_s` is the previous
`end_s`) and the last `end_s` must stay at or below 59.5. Animations are keyed to each scene's
progress, so a longer or shorter scene stretches its beats with no component edits. Captions
are cut from `vo_text` at sentence ends and spread across `vo_start_s` .. `vo_end_s`; set those
to where the voice in `vo.wav` really starts and ends.
