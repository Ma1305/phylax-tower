"""STOP phrase building, ElevenLabs TTS and the decisions change-stream watcher."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import requests
from bson import ObjectId

from holdshort.agents._stream import StreamAgent
from holdshort.env import ROOT, redact
from holdshort.listener.voice import voice_enabled

API = "https://api.elevenlabs.io/v1"
ALERT_DIR = Path(os.environ.get("HOLDSHORT_VOICE_ALERT_DIR", "") or ROOT / "logs" / "voice_alerts")
# Rachel (calm, even): same premade voice as the tower in scripts/audio/generate.py.
DEFAULT_VOICE = "21m00Tcm4TlvDq8ikWAM"
TIMEOUT_S = 30

_DIGITS = "zero one two three four five six seven eight niner".split()
_WORDS = {"TRUCK": "Truck", "AC": "Air Canada", "DL": "Delta", "UA": "United", "OPS": "Ops"}


def spoken_callsign(cs: str) -> str:
    """AC8646 -> "Air Canada eight six four six", TRUCK1 -> "Truck one"."""
    m = re.fullmatch(r"([A-Z]+)(\d*)", cs or "")
    if not m:
        return cs
    prefix, digits = m.groups()
    head = _WORDS.get(prefix, " ".join(prefix) if len(prefix) <= 3 else prefix.title())
    return " ".join([head, *(_DIGITS[int(d)] for d in digits)]).strip()


def _spoken_runway(rwy: str | None) -> str:
    return " ".join(_DIGITS[int(d)] for d in rwy) if rwy and rwy.isdigit() else (rwy or "")


def phrase(target: list[str], runway: str | None, conflicts_with: str | None = None) -> str:
    """The words spoken for one STOP (callsigns by name, digits the radio way).

    The STOP goes to the targets that are *not* the conflicting traffic; the conflicting
    traffic (first callsign of ``conflicts_with``, e.g. the landing jet) gets a traffic
    advisory instead: "Truck one, STOP. Hold short runway zero four. Air Canada eight six
    four six, traffic Truck one holding short runway zero four."
    """
    m = re.match(r"\s*([A-Z]+\d+)\b", conflicts_with or "")
    other = m.group(1) if m else None
    stopped = [c for c in target if c != other] or list(target)
    who = ", ".join(spoken_callsign(c) for c in stopped) or "All vehicles"
    rwy = _spoken_runway(runway)
    text = f"{who}, STOP. Hold short runway {rwy}." if rwy else f"{who}, STOP. Hold position."
    if other and other in target and other not in stopped:
        where = f" holding short runway {rwy}" if rwy else " holding position"
        text += f" {spoken_callsign(other)}, traffic {who}{where}."
    return text


def alert_path(decision_id: str) -> Path:
    if not ObjectId.is_valid(decision_id):
        raise ValueError("decision_id must be an ObjectId")
    return ALERT_DIR / f"{decision_id}.mp3"


def speak(text: str, dst: Path) -> Path:
    """ElevenLabs text-to-speech -> mp3 at ``dst`` (raises RuntimeError on failure)."""
    key = os.environ.get("ELEVENLABS_API_KEY", "").strip()
    voice = os.environ.get("ELEVENLABS_VOICE_ALERT", "").strip() or DEFAULT_VOICE
    model = os.environ.get("ELEVENLABS_MODEL", "").strip() or "eleven_multilingual_v2"
    try:
        r = requests.post(
            f"{API}/text-to-speech/{voice}",
            params={"output_format": "mp3_44100_128"},
            headers={"xi-api-key": key, "accept": "audio/mpeg"},
            json={"text": text, "model_id": model,
                  "voice_settings": {"stability": 0.6, "similarity_boost": 0.75}},
            timeout=TIMEOUT_S,
        )
    except requests.RequestException as e:
        raise RuntimeError(f"ElevenLabs request failed: {redact(e)}") from None
    if r.status_code != 200:
        raise RuntimeError(f"ElevenLabs HTTP {r.status_code}: {redact(r.text[:200])}")
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_suffix(".part")
    tmp.write_bytes(r.content)
    tmp.replace(dst)  # atomic: the API never serves half a file
    return dst


def _play_local(path: Path) -> None:
    for cmd in (["ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet"], ["mpv", "--no-video"]):
        if shutil.which(cmd[0]):
            subprocess.Popen([*cmd, str(path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return


class VoiceAlert(StreamAgent):
    """Speaks every executed STOP. Output service only: reads decisions + intents."""

    name = "voice_alert"
    collection = "decisions"

    def __init__(self, db, max_await_ms: int = 300, *, enabled: bool | None = None):
        super().__init__(db, max_await_ms)
        self.enabled = voice_enabled() if enabled is None else enabled
        self.spoken: list[tuple[str, str, Path | None]] = []  # (decision_id, text, file)

    def pipeline(self) -> list[dict]:
        return [{"$match": {"operationType": "insert",
                            "fullDocument.intent_action": "stop",
                            "fullDocument.outcome": "executed"}}]

    def on_ready(self) -> None:
        self.log.info("voice %s", "ON (ElevenLabs TTS)" if self.enabled
                      else "OFF (VOICE_ENABLED false or no ELEVENLABS_API_KEY): text only")

    def handle(self, doc: dict[str, Any]) -> None:
        intent = self.db[f"intents_{doc.get('agent')}"].find_one({"_id": doc.get("intent_id")}) or {}
        target = intent.get("target") or doc.get("notified") or []
        text = phrase(target, intent.get("runway"), intent.get("conflicts_with"))
        did = str(doc["_id"])
        out: Path | None = None
        if self.enabled:
            try:
                out = speak(text, alert_path(did))
                self.log.info("SPOKE %s %r -> %s", did, text, out.name)
                if os.environ.get("VOICE_ALERT_PLAY", "").strip().lower() in {"1", "true", "yes"}:
                    _play_local(out)
            except RuntimeError as e:
                self.log.warning("TTS failed for %s (%s); text: %r", did, e, text)
        else:
            self.log.info("STOP %s (voice off) would say: %r", did, text)
        self.spoken.append((did, text, out))
