"""Shared paths and .env loading for the audio scripts.

.env lookup: $HOLDSHORT_ENV, then <repo>/.env, then ~/plane-management/.env.
Values already in the process environment win (so VOICE_ENABLED=true can be
set for one run without editing the file).
"""
import json
import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[2]
SCENARIO = ROOT / "scenarios" / "laguardia"
SCRIPT = SCENARIO / "script.json"
AUDIO = SCENARIO / "audio"
CLEAN = AUDIO / "clean"
LEVELS = ("mild", "medium", "brutal")


def load_env() -> None:
    candidates = []
    if os.environ.get("HOLDSHORT_ENV"):
        candidates.append(Path(os.environ["HOLDSHORT_ENV"]))
    candidates += [ROOT / ".env", Path.home() / "plane-management" / ".env"]
    for p in candidates:
        if p.is_file():
            load_dotenv(p, override=False)
            return


def transmissions() -> list[dict]:
    events = json.loads(SCRIPT.read_text())["events"]
    return [e for e in events if e.get("type") == "transmission"]
