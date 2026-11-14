# Radio audio for the LaGuardia scenario

No real ATC audio is used. Every clip is synthesized from `scenarios/laguardia/script.json`.

```bash
uv run python scripts/audio/generate.py            # ElevenLabs TTS -> audio/clean/  (needs VOICE_ENABLED=true + ELEVENLABS_API_KEY)
uv run python scripts/audio/generate.py --offline  # local ffmpeg/flite placeholder voices, no key needed
scripts/audio/degrade.sh                           # -> audio/{mild,medium,brutal}/ + overlap clip + manifest.json
```

- One voice per speaker: TOWER, AC8646 and TRUCK1. You can override the ElevenLabs voices with
  `ELEVENLABS_VOICE_TOWER`, `ELEVENLABS_VOICE_AC8646`, `ELEVENLABS_VOICE_TRUCK1` and `ELEVENLABS_MODEL`.
- If voice is disabled or the key is missing, `generate.py` prints why and exits 0 without writing anything.
- Clip ids match the `id` of each transmission in `script.json` (`t01.wav` ... `t10.wav`).
  `overlap_t06_t07.wav` is t06 (the unclear TRUCK1 call) stepped on by t07 (AC8646), starting 1.2 s in.
- Degradation per level: band-pass, compression, pink noise, a squelch burst at key-up and release, then
  8 kHz mono 16-bit WAV. Measured speech-to-noise on t02 is about 26 dB (mild), 18 dB (medium) and 11 dB (brutal).
- The WAVs are gitignored. `audio/manifest.json` is committed and lists every clip at every level with its
  duration and sample rate. `tts_engine` records which engine made the clean clips. Rerun both scripts after
  changing the script or switching engines, because durations change.
