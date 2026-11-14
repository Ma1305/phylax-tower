"""Generate clean TTS clips for the LaGuardia script.

    uv run python scripts/audio/generate.py            # ElevenLabs (needs VOICE_ENABLED=true + key)
    uv run python scripts/audio/generate.py --offline  # local ffmpeg/flite placeholder voices

Writes scenarios/laguardia/audio/clean/<id>.wav (mono, 16-bit, 22.05 kHz),
one per transmission in script.json, plus clean/source.json naming the engine.
With VOICE_ENABLED false or no ELEVENLABS_API_KEY it prints why and exits 0
(unless --offline is given). The API key is never printed.
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import CLEAN, load_env, transmissions  # noqa: E402

# ElevenLabs premade voices, one distinct voice per speaker. Override with
# ELEVENLABS_VOICE_TOWER / ELEVENLABS_VOICE_AC8646 / ELEVENLABS_VOICE_TRUCK1.
ELEVEN_VOICES = {
    "TOWER": "21m00Tcm4TlvDq8ikWAM",   # Rachel: calm, even
    "AC8646": "onwK4e9ZLuTAKqWW03F9",  # Daniel: steady, flat
    "TRUCK1": "pNInz6obpgDQGcFmaJgB",  # Adam: deeper, rougher
}
# Offline placeholders (ffmpeg's built-in flite), also distinct per speaker.
FLITE_VOICES = {"TOWER": "slt", "AC8646": "kal16", "TRUCK1": "awb"}
SAMPLE_RATE = 22050


def to_wav(src: Path | str, dst: Path, lavfi: bool = False) -> None:
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y"]
    cmd += ["-f", "lavfi", "-i", str(src)] if lavfi else ["-i", str(src)]
    cmd += ["-ac", "1", "-ar", str(SAMPLE_RATE), "-sample_fmt", "s16", str(dst)]
    subprocess.run(cmd, check=True)


def eleven(text: str, voice: str, key: str, dst: Path) -> None:
    r = requests.post(
        f"https://api.elevenlabs.io/v1/text-to-speech/{voice}",
        params={"output_format": "mp3_44100_128"},
        headers={"xi-api-key": key, "accept": "audio/mpeg"},
        json={"text": text,
              "model_id": os.environ.get("ELEVENLABS_MODEL", "").strip()
              or "eleven_multilingual_v2",
              "voice_settings": {"stability": 0.5, "similarity_boost": 0.75}},
        timeout=90,
    )
    if r.status_code != 200:
        raise RuntimeError(f"ElevenLabs HTTP {r.status_code}: {r.text[:200]}")
    with tempfile.NamedTemporaryFile(suffix=".mp3") as tmp:
        tmp.write(r.content)
        tmp.flush()
        to_wav(tmp.name, dst)


def flite(text: str, voice: str, dst: Path) -> None:
    safe = text.replace("\\", " ").replace("'", "").replace(":", " ").replace(",", "\\,")
    to_wav(f"flite=text='{safe}':voice={voice}", dst, lavfi=True)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--offline", action="store_true",
                    help="use ffmpeg/flite placeholder voices instead of ElevenLabs")
    args = ap.parse_args()
    load_env()

    enabled = os.environ.get("VOICE_ENABLED", "false").strip().lower() in ("1", "true", "yes", "on")
    key = os.environ.get("ELEVENLABS_API_KEY", "").strip()
    engine = "flite-offline" if args.offline else "elevenlabs"
    if not args.offline and not (enabled and key):
        why = "VOICE_ENABLED is not true" if not enabled else "ELEVENLABS_API_KEY is not set"
        print(f"generate.py: skipping ElevenLabs TTS ({why}). Nothing was generated.\n"
              "  Set VOICE_ENABLED=true and ELEVENLABS_API_KEY in the .env to use ElevenLabs,\n"
              "  or run with --offline for local placeholder voices.")
        return 0

    # Build into a staging dir so a failed run leaves the previous clips intact.
    CLEAN.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=".staging-", dir=CLEAN.parent))
    for t in transmissions():
        dst = stage / f"{t['id']}.wav"
        spk = t["speaker"]
        if args.offline:
            flite(t["text"], FLITE_VOICES[spk], dst)
        else:
            voice = os.environ.get(f"ELEVENLABS_VOICE_{spk}", "").strip() or ELEVEN_VOICES[spk]
            try:
                eleven(t["text"], voice, key, dst)
            except Exception as exc:  # message never includes the key
                print(f"generate.py: FAILED on {t['id']}: {str(exc).replace(key, '<key>')}")
                shutil.rmtree(stage)
                return 1
        print(f"  clean/{dst.name}  [{spk}] {t['text']}")
    shutil.rmtree(CLEAN)
    stage.rename(CLEAN)
    (CLEAN / "source.json").write_text(json.dumps({"engine": engine}) + "\n")
    print(f"generate.py: wrote {len(transmissions())} clean clips with {engine}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
