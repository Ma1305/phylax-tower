"""Turn executed stops / approved instructions into outbound audio + a synthetic transmission.

Watches ``decisions`` (insert-only) with a resumable change stream. For each decision that
``should_speak``: synthesise ``decision.text`` (ElevenLabs TTS when voice is enabled, else an
ffmpeg two-tone placeholder), store the bytes in GridFS bucket ``audio``, insert an
``audio_clips`` document (direction "out") and an outbound ``transmissions`` document
(synthetic, ``decision_id`` set). Exactly once per decision: guarded on ``decision_id``.
"""

from __future__ import annotations

import io
import logging
import math
import os
import shutil
import struct
import subprocess
import wave
from datetime import UTC, datetime, timedelta
from typing import Any

import gridfs
import requests
from pymongo.database import Database

from holdshort.agents._stream import StreamAgent
from holdshort.contracts import HOLDSHORT, UNKNOWN_SPEAKER, AudioClip, Transmission, to_mongo
from holdshort.env import redact
from holdshort.listener.voice import voice_enabled
from holdshort.world import stream_of

log = logging.getLogger("holdshort.speaker")

API = "https://api.elevenlabs.io/v1"
# Rachel (calm, even): the default voice the v1 voice alert used.
DEFAULT_VOICE = "21m00Tcm4TlvDq8ikWAM"
TIMEOUT_S = 30
SPOKEN_ACTIONS = ("stop", "propose_instruction")


def now() -> datetime:
    return datetime.now(UTC)


# ------------------------------------------------------------------ synthesis


def tts_elevenlabs(text: str) -> tuple[bytes, str]:
    """ElevenLabs text-to-speech -> (mp3 bytes, mime). Raises RuntimeError on failure."""
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
    if r.status_code != 200 or not r.content:
        raise RuntimeError(f"ElevenLabs HTTP {r.status_code}: {redact(r.text[:200])}")
    return r.content, "audio/mpeg"


def _python_tones(rate: int = 16000) -> bytes:
    frames = bytearray()
    for freq in (880, 660):
        for i in range(int(rate * 0.3)):
            frames += struct.pack("<h", int(12000 * math.sin(2 * math.pi * freq * i / rate)))
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(bytes(frames))
    return buf.getvalue()


def placeholder_tone() -> tuple[bytes, str]:
    """A short two-tone wav (ffmpeg; pure-Python fallback) so there is always audio."""
    if shutil.which("ffmpeg"):
        cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error",
               "-f", "lavfi", "-i", "sine=frequency=880:duration=0.3",
               "-f", "lavfi", "-i", "sine=frequency=660:duration=0.3",
               "-filter_complex", "[0:a][1:a]concat=n=2:v=0:a=1",
               "-ar", "16000", "-ac", "1", "-f", "wav", "pipe:1"]
        try:
            out = subprocess.run(cmd, capture_output=True, timeout=20, check=True).stdout
            if out:
                return out, "audio/wav"
        except (OSError, subprocess.SubprocessError) as e:
            log.warning("ffmpeg placeholder failed (%s); using python tones", type(e).__name__)
    return _python_tones(), "audio/wav"


def synthesize(text: str) -> tuple[bytes, str]:
    """(audio bytes, mime). TTS when voice is enabled, else (or on failure) the placeholder."""
    if voice_enabled():
        try:
            return tts_elevenlabs(text)
        except RuntimeError as e:
            log.warning("text-to-speech failed, using placeholder: %s", e)
    return placeholder_tone()


def duration_of(data: bytes, mime: str) -> float:
    if mime == "audio/wav":
        try:
            with wave.open(io.BytesIO(data)) as w:
                return round(w.getnframes() / float(w.getframerate()), 3)
        except (wave.Error, EOFError):
            pass
    return round(len(data) * 8 / 128_000, 3)  # mp3_44100_128


# ------------------------------------------------------------------- speaking


def should_speak(decision: dict[str, Any]) -> bool:
    if decision.get("outcome") != "executed":
        return False
    action = decision.get("intent_action")
    if action not in SPOKEN_ACTIONS:
        return False
    if action == "propose_instruction" and not decision.get("approved_by"):
        return False
    return bool((decision.get("text") or "").strip())


def speak_decision(db: Database, decision: dict[str, Any]) -> dict | None:
    """Speak one decision. Returns the transmission inserted, or None (skipped / done before)."""
    if not should_speak(decision):
        return None
    if db.transmissions.find_one({"decision_id": decision["_id"]}, {"_id": 1}):
        return None  # exactly once
    text = decision["text"].strip()
    controller_id = decision.get("controller_id")
    stream_id = decision.get("stream_id") or (stream_of(db, controller_id) if controller_id
                                              else None)
    if not stream_id:
        log.warning("decision %s has no stream to speak on; skipped", decision["_id"])
        return None
    tower_id = decision.get("tower_id")
    is_stop = decision.get("intent_action") == "stop"

    data, mime = synthesize(text)
    t_start = now()
    dur = duration_of(data, mime)
    t_end = t_start + timedelta(seconds=dur)
    audio_id = gridfs.GridFSBucket(db, bucket_name="audio").upload_from_stream(
        f"out-{decision['_id']}", data, metadata={"mime": mime, "decision_id": decision["_id"]})

    clip = to_mongo(AudioClip(
        tower_id=tower_id, stream_id=stream_id, direction="out", t_start=t_start, t_end=t_end,
        duration_s=dur, audio_id=audio_id, mime=mime, truth_text=text, speaker=HOLDSHORT,
        status="done", created_at=t_start,
    ))
    clip_id = db.audio_clips.insert_one(clip).inserted_id

    targets = decision.get("target") or []
    tx = to_mongo(Transmission(
        tower_id=tower_id, stream_id=stream_id, controller_id=controller_id, t=t_start,
        t_end=t_end, direction="out", **{"from": HOLDSHORT},
        to=targets[0] if targets else UNKNOWN_SPEAKER,
        kind="stop" if is_stop else "other",
        instruction="hold_short" if is_stop else "none",
        lane_id=decision.get("lane_id"), transcript=text, confidence=1.0,
        readback="not_applicable", verified=True, clip_id=clip_id, audio_id=audio_id,
        synthetic=True, decision_id=decision["_id"],
    ))
    # re-check right before the insert (a second speaker could have raced us)
    if db.transmissions.find_one({"decision_id": decision["_id"]}, {"_id": 1}):
        return None
    tx["_id"] = db.transmissions.insert_one(tx).inserted_id
    db.audio_clips.update_one({"_id": clip_id}, {"$set": {"transmission_id": tx["_id"]}})
    log.info("spoke decision %s on %s: %s", decision["_id"], stream_id, text)
    return tx


class Speaker(StreamAgent):
    """Watches decision inserts and speaks executed stops / approved instructions."""

    name = "speaker"
    collection = "decisions"
    operation_types = ("insert",)

    def pipeline(self) -> list[dict]:
        return [{"$match": {"operationType": "insert", "ns.coll": "decisions",
                            "fullDocument.outcome": "executed",
                            "fullDocument.intent_action": {"$in": list(SPOKEN_ACTIONS)}}}]

    def on_ready(self) -> None:
        self.log.info("voice %s", "ON (ElevenLabs TTS)" if voice_enabled()
                      else "OFF (VOICE_ENABLED false or no ELEVENLABS_API_KEY): placeholder tone")

    def handle(self, doc: dict[str, Any]) -> None:
        speak_decision(self.db, doc)
