"""ElevenLabs voice isolation + speech-to-text for radio clips.

Only used when VOICE_ENABLED is true and ELEVENLABS_API_KEY is set; otherwise the
listener runs in text mode. Secrets are never printed (errors go through redact()).
"""

from __future__ import annotations

import math
import os
import tempfile
from pathlib import Path

import requests

from holdshort.env import redact

API = "https://api.elevenlabs.io/v1"
STT_MODEL = os.environ.get("ELEVENLABS_STT_MODEL", "").strip() or "scribe_v1"
TIMEOUT_S = 120


def voice_enabled() -> bool:
    """True when VOICE_ENABLED is truthy and an ElevenLabs key is present."""
    flag = os.environ.get("VOICE_ENABLED", "").strip().lower() in {"1", "true", "yes", "on"}
    return flag and bool(os.environ.get("ELEVENLABS_API_KEY", "").strip())


def _key() -> str:
    return os.environ.get("ELEVENLABS_API_KEY", "").strip()


def clean(path: str | Path) -> Path:
    """Return a voice-isolated copy of the clip, or the original path (passthrough).

    Passthrough happens when voice is disabled or the isolation call fails, so a
    broken isolation endpoint never stops the listener.
    """
    path = Path(path)
    if not voice_enabled():
        return path
    try:
        with path.open("rb") as f:
            r = requests.post(
                f"{API}/audio-isolation",
                headers={"xi-api-key": _key()},
                files={"audio": (path.name, f, "audio/wav")},
                timeout=TIMEOUT_S,
            )
        if r.status_code != 200 or not r.content:
            print(f"listener: voice isolation unavailable (HTTP {r.status_code}); passthrough")
            return path
        suffix = ".mp3" if r.headers.get("content-type", "").startswith("audio/mpeg") else ".wav"
        out = Path(tempfile.mkstemp(prefix=f"{path.stem}_clean_", suffix=suffix)[1])
        out.write_bytes(r.content)
        return out
    except requests.RequestException as e:
        print(f"listener: voice isolation failed ({redact(e)}); passthrough")
        return path


def _confidence(body: dict) -> float:
    """Mean per-word probability from ElevenLabs logprobs (fallback: language probability)."""
    words = [w for w in body.get("words") or [] if w.get("type", "word") == "word"]
    probs = [math.exp(w["logprob"]) for w in words if isinstance(w.get("logprob"), int | float)]
    if probs:
        return max(0.0, min(1.0, sum(probs) / len(probs)))
    lp = body.get("language_probability")
    return float(lp) if isinstance(lp, int | float) else 0.0


def transcribe(path: str | Path) -> tuple[str, float]:
    """ElevenLabs speech-to-text -> (text, confidence 0..1).

    Returns ("", 0.0) when voice is disabled or the call fails; the listener then
    stores an unverified, flagged "say again" transmission rather than guessing.
    """
    path = Path(path)
    if not voice_enabled():
        return "", 0.0
    try:
        with path.open("rb") as f:
            r = requests.post(
                f"{API}/speech-to-text",
                headers={"xi-api-key": _key()},
                data={"model_id": STT_MODEL, "language_code": "en", "tag_audio_events": "false"},
                files={"file": (path.name, f)},
                timeout=TIMEOUT_S,
            )
        if r.status_code != 200:
            print(f"listener: speech-to-text HTTP {r.status_code}: {redact(r.text[:200])}")
            return "", 0.0
        body = r.json()
        return (body.get("text") or "").strip(), _confidence(body)
    except (requests.RequestException, ValueError) as e:
        print(f"listener: speech-to-text failed ({redact(e)})")
        return "", 0.0
