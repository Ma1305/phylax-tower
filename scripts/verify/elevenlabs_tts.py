"""Check 7: generate one short TTS clip if ELEVENLABS_API_KEY is set, otherwise report pending."""
import os
import sys
from pathlib import Path

import requests

from _env import ROOT  # noqa: F401  (loads .env)

key = os.environ.get("ELEVENLABS_API_KEY", "").strip()
if not key:
    print("PENDING 7: ElevenLabs pending (ELEVENLABS_API_KEY not set; VOICE_ENABLED=false path is used)")
    sys.exit(0)
voice = os.environ.get("ELEVENLABS_VOICE_ID", "JBFqnCBsd6RMkjVDRZzb")
r = requests.post(f"https://api.elevenlabs.io/v1/text-to-speech/{voice}",
                  headers={"xi-api-key": key, "accept": "audio/mpeg"},
                  json={"text": "Hold short of runway two seven left.", "model_id": "eleven_multilingual_v2"},
                  timeout=60)
if r.status_code != 200:
    print(f"FAIL 7: HTTP {r.status_code}: {r.text[:300]}")
    sys.exit(1)
out = Path(__file__).parent / "out"
out.mkdir(exist_ok=True)
(out / "tts_test.mp3").write_bytes(r.content)
print(f"PASS 7: wrote {len(r.content)} bytes to {out / 'tts_test.mp3'}")
