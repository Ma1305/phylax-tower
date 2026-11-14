"""Multi-stream listener: inbound queued audio_clips -> Transmissions, many streams at once.

    MultiStreamListener(db, workers=8).run()

- A change stream on ``audio_clips`` (inserts of direction "in" / status "queued") plus a
  catch-up query at start (and every ``RESCAN_S`` seconds) find work.
- Each clip is claimed atomically (``find_one_and_update`` queued -> processing), so two
  listeners never take the same clip.
- One stream is drained by one worker at a time, oldest ``t_start`` first; different
  streams run in parallel on a thread pool.
- Per clip: voice isolation + ElevenLabs speech-to-text when voice is on and the clip has
  audio; else (or when that fails) the clip's ``truth_text`` at confidence 0.95; nothing
  usable -> an "unclear - say again" transmission at confidence 0. Then parse, normalise
  callsigns, flag confusable callsigns, readback check, insert, mark the clip done.

All data access goes through ``scoped_db(db, "listener")``. The raw handle is only used by
the change-stream plumbing (``StreamAgent``: watch + its ``agent_state`` resume token).
"""

from __future__ import annotations

import os
import re
import tempfile
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pymongo import ASCENDING, DESCENDING, ReturnDocument
from pymongo.database import Database

from holdshort import world
from holdshort.access import scoped_db
from holdshort.agents._stream import StreamAgent
from holdshort.contracts import Transmission, to_mongo
from holdshort.env import redact
from holdshort.listener import voice
from holdshort.listener.agent import check_readback, insert
from holdshort.listener.normalize import UNKNOWN
from holdshort.listener.parse import FLAG_UNCLEAR, STATIONS, LlmFn, get_labeller, parse

TRUTH_CONFIDENCE = 0.95
RESCAN_S = 10.0
LIVE_CACHE_S = 2.0
QUEUED = {"direction": "in", "status": "queued"}

SttFn = Callable[[bytes, str | None], tuple[str, float]]
"""(audio bytes, mime) -> (text, confidence). Raise or return "" when it fails."""

_EXT = {"audio/mpeg": ".mp3", "audio/mp3": ".mp3", "audio/wav": ".wav", "audio/x-wav": ".wav",
        "audio/ogg": ".ogg", "audio/flac": ".flac", "audio/mp4": ".m4a"}


def elevenlabs_stt(data: bytes, mime: str | None) -> tuple[str, float]:
    """Voice isolation, then ElevenLabs speech-to-text (holdshort.listener.voice)."""
    fd, name = tempfile.mkstemp(prefix="clip_", suffix=_EXT.get(mime or "", ".wav"))
    path = Path(name)
    cleaned = path
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        cleaned = voice.clean(path)
        text, conf = voice.transcribe(cleaned)
        if not text.strip():
            raise RuntimeError("speech-to-text returned no text")
        return text, conf
    finally:
        for p in {path, cleaned}:
            p.unlink(missing_ok=True)


# ------------------------------------------------------------------ confusable


def _split(cs: str) -> tuple[str, str]:
    m = re.fullmatch(r"([A-Z]*)(\d*)([A-Z]*)", cs)
    return (m.group(1), m.group(2)) if m and not m.group(3) else (cs, "")


def confusable(a: str, b: str) -> bool:
    """Same prefix, same-length digits made of the same digits (AC8646 / AC8464 / AC6846)
    or differing in one place (AC8646 / AC8645)."""
    if a == b:
        return False
    pa, da = _split(a)
    pb, db_ = _split(b)
    if pa != pb or not da or len(da) != len(db_):
        return False
    return set(da) == set(db_) or sum(x != y for x, y in zip(da, db_, strict=True)) == 1


# -------------------------------------------------------------------- processor


class ClipProcessor:
    """Turns one claimed clip into one Transmission (thread-safe)."""

    def __init__(self, db: Database, *, llm: LlmFn | None = None,
                 stt: SttFn | None | str = "auto"):
        self.raw = db
        self.db = scoped_db(db, "listener")
        self.llm = llm
        self.stt: SttFn | None = (
            (elevenlabs_stt if voice.voice_enabled() else None) if stt == "auto" else stt
        )  # type: ignore[assignment]
        self._lock = threading.Lock()
        self._live: tuple[float, set[str]] = (0.0, set())
        self._objs: tuple[float, dict[str, str | None]] = (0.0, {})
        self._aliases: tuple[float, dict[str, str]] = (0.0, {})

    # -- lookups (cached briefly) -------------------------------------------------

    def live_callsigns(self) -> set[str]:
        with self._lock:
            at, cs = self._live
        if time.monotonic() - at < LIVE_CACHE_S:
            return cs
        cs = {c for c in self.db.objects.distinct("callsign") if isinstance(c, str) and c}
        with self._lock:
            self._live = (time.monotonic(), cs)
        return cs

    def live_objects(self) -> dict[str, str | None]:
        """Live callsign -> object kind (aircraft / vehicle), plus known callsigns."""
        with self._lock:
            at, objs = self._objs
        if time.monotonic() - at < LIVE_CACHE_S:
            return objs
        objs = {}
        for d in self.db.callsigns.find({}, {"callsign": 1, "kind": 1}):
            cs = d.get("callsign") or d.get("_id")
            if isinstance(cs, str) and cs:
                objs[cs.upper()] = d.get("kind")
        for d in self.db.objects.find({}, {"callsign": 1, "kind": 1}):
            cs = d.get("callsign")
            if isinstance(cs, str) and cs:
                objs[cs.upper()] = d.get("kind")
        with self._lock:
            self._objs = (time.monotonic(), objs)
        return objs

    def aliases(self) -> dict[str, str]:
        with self._lock:
            at, al = self._aliases
        if time.monotonic() - at < world.CACHE_TTL_S:
            return al
        al = {}
        for d in self.db.callsigns.find({}, {"spoken": 1, "callsign": 1}):
            cs = d.get("callsign") or d.get("_id")
            if isinstance(cs, str) and d.get("spoken"):
                al[str(d["spoken"]).lower()] = cs.upper()
        with self._lock:
            self._aliases = (time.monotonic(), al)
        return al

    def last_clearance(self, stream_id: str, callsign: str, t: datetime) -> dict | None:
        return self.db.transmissions.find_one(
            {"stream_id": stream_id, "kind": "clearance", "to": callsign, "t": {"$lte": t}},
            sort=[("t", DESCENDING)],
        )

    def audio_bytes(self, audio_id: Any) -> bytes:
        chunks = self.db["audio.chunks"].find({"files_id": audio_id}).sort("n", ASCENDING)
        return b"".join(bytes(c["data"]) for c in chunks)

    # -- the pipeline ---------------------------------------------------------------

    def hear(self, clip: dict) -> tuple[str, float]:
        """(text, confidence): speech-to-text, else truth_text, else ("", 0)."""
        if clip.get("audio_id") is not None and self.stt is not None:
            try:
                text, conf = self.stt(self.audio_bytes(clip["audio_id"]), clip.get("mime"))
                if text and text.strip():
                    return text.strip(), max(0.0, min(1.0, float(conf)))
            except Exception as e:  # noqa: BLE001 - fall back to the truth text
                print(f"listener: speech-to-text failed for {clip['_id']}: "
                      f"{type(e).__name__}: {redact(e)}")
        truth = (clip.get("truth_text") or "").strip()
        if truth:
            return truth, TRUTH_CONFIDENCE
        return "", 0.0

    def build(self, clip: dict) -> Transmission:
        stream = world.stream_doc(self.raw, clip["stream_id"]) or {}
        station = stream.get("station")
        controller_id = stream.get("controller_id") or world.supervisor_of(
            self.raw, clip.get("tower_id") or world.DEFAULT_TOWER) or "unknown"
        text, conf = self.hear(clip)
        speaker = clip.get("speaker")
        if speaker and speaker.upper() in STATIONS and station:
            speaker = station
        fields = {
            "tower_id": clip["tower_id"], "stream_id": clip["stream_id"],
            "controller_id": controller_id, "t_end": clip.get("t_end"), "direction": "in",
            "clip_id": clip["_id"], "audio_id": clip.get("audio_id"), "synthetic": False,
            "decision_id": None,
        }
        context = {"t": clip["t_start"], "speaker": speaker, "aliases": self.aliases(),
                   "live": self.live_objects(),
                   "station": station, "fields": fields}
        tx = parse(text, context, confidence=conf if text else 0.0,
                   llm=self.llm or get_labeller())
        update: dict[str, Any] = {}
        if tx.flag != FLAG_UNCLEAR:
            lane = world.lane_for_designator(self.raw, clip["tower_id"], tx.runway) \
                or world.lane_for_designator(self.raw, clip["tower_id"], tx.at)
            update["lane_id"] = lane
            last = self.last_clearance(clip["stream_id"], tx.from_, clip["t_start"]) \
                if tx.kind == "readback" else None
            update["readback"] = check_readback(tx, last)
        if not tx.flag:
            live = self.live_callsigns()
            for cs in (tx.to, tx.from_):
                if cs in STATIONS or cs == UNKNOWN:
                    continue
                other = next((o for o in sorted(live) if confusable(cs, o)), None)
                if other:
                    update["flag"] = f"confusable: {other}"
                    break
        tx = tx.model_copy(update=update)
        return Transmission.model_validate(to_mongo(tx))  # re-run validators

    def process(self, clip: dict) -> Any:
        """Build + insert the transmission and mark the clip done (failed on error)."""
        try:
            tx = self.build(clip)
            tx_id = insert(tx, self.db)
        except Exception as e:  # noqa: BLE001 - a failed clip never blocks its stream
            self.db.audio_clips.update_one(
                {"_id": clip["_id"]},
                {"$set": {"status": "failed", "error": f"{type(e).__name__}: {redact(e)}"[:500]}},
            )
            print(f"listener: clip {clip['_id']} failed: {type(e).__name__}: {redact(e)}")
            return None
        self.db.audio_clips.update_one(
            {"_id": clip["_id"]}, {"$set": {"status": "done", "transmission_id": tx_id}}
        )
        return tx_id

    def claim(self, stream_id: str) -> dict | None:
        """Atomically take the oldest queued inbound clip of a stream."""
        return self.db.audio_clips.find_one_and_update(
            {**QUEUED, "stream_id": stream_id},
            {"$set": {"status": "processing"}},
            sort=[("t_start", ASCENDING), ("_id", ASCENDING)],
            return_document=ReturnDocument.AFTER,
        )


# ------------------------------------------------------------------ the worker


class MultiStreamListener(StreamAgent):
    """Change stream + catch-up -> per-stream drains on a thread pool."""

    name = "listener"
    collection = "audio_clips"
    operation_types = ("insert", "update", "replace")

    def __init__(self, db: Database, workers: int = 8, *, llm: LlmFn | None = None,
                 stt: SttFn | None | str = "auto", max_await_ms: int = 300):
        super().__init__(db, max_await_ms=max_await_ms)
        self.processor = ClipProcessor(db, llm=llm, stt=stt)
        self.pool = ThreadPoolExecutor(max_workers=max(1, workers),
                                       thread_name_prefix="listener")
        self._lock = threading.Lock()
        self._active: set[str] = set()
        self._again: set[str] = set()
        self.done = 0

    def pipeline(self) -> list[dict]:
        return [{"$match": {
            "ns.coll": "audio_clips",
            "operationType": {"$in": list(self.operation_types)},
            "fullDocument.direction": "in",
            "fullDocument.status": "queued",
        }}]

    def handle(self, doc: dict[str, Any]) -> None:
        if doc.get("stream_id"):
            self.schedule(doc["stream_id"])

    def catch_up(self) -> None:
        for sid in self.processor.db.audio_clips.distinct("stream_id", QUEUED):
            self.schedule(sid)

    def on_ready(self) -> None:
        self.catch_up()
        threading.Thread(target=self._rescan, name="listener-rescan", daemon=True).start()

    def _rescan(self) -> None:
        while not self._stop.wait(RESCAN_S):
            try:
                self.catch_up()
            except Exception:  # noqa: BLE001
                self.log.exception("catch-up scan failed")

    def schedule(self, stream_id: str) -> None:
        with self._lock:
            if stream_id in self._active:
                self._again.add(stream_id)
                return
            self._active.add(stream_id)
        try:
            self.pool.submit(self._drain, stream_id)
        except RuntimeError:  # pool shut down
            with self._lock:
                self._active.discard(stream_id)

    def _drain(self, stream_id: str) -> None:
        try:
            while not self._stop.is_set():
                clip = self.processor.claim(stream_id)
                if clip is None:
                    with self._lock:
                        if stream_id in self._again:
                            self._again.discard(stream_id)
                            continue
                        self._active.discard(stream_id)
                        return
                started = datetime.now(UTC)
                tx_id = self.processor.process(clip)
                self.done += 1
                self.log.info("clip %s on %s -> %s (%.2fs)", clip["_id"], stream_id,
                              tx_id or "FAILED",
                              (datetime.now(UTC) - started).total_seconds())
        except Exception:  # noqa: BLE001
            self.log.exception("drain of %s failed", stream_id)
        with self._lock:  # stopped or crashed: let a later schedule() start a new drain
            self._active.discard(stream_id)
            self._again.discard(stream_id)

    def run(self) -> None:
        try:
            super().run()
        finally:
            self.pool.shutdown(wait=True)
