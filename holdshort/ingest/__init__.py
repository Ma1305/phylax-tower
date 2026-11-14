"""The /ingest/* logic (docs/v2/SIMULATOR_CONTRACT.md). The router in
holdshort/api/routers/ingest.py only parses HTTP and calls these functions.

Every function takes the database first and raises ``IngestError(status, detail)`` for the
contract's error codes (400, 404, 413, 422).
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from bson import ObjectId
from gridfs import GridFSBucket
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, ValidationError
from pymongo import UpdateOne
from pymongo.database import Database

from holdshort import contracts as c
from holdshort import world as w
from holdshort.events import log_state_event

MAX_OBJECTS = 500
MAX_AUDIO_S = 60.0
MAX_AUDIO_BYTES = 5 * 1024 * 1024
MAX_SKEW_S = 120.0
RUNWAY_AIRBORNE_MAX_FT = 200.0
AUDIO_BUCKET = "audio"
AUDIO_MIMES = ("audio/wav", "audio/x-wav", "audio/wave", "audio/mpeg", "audio/mp3", "audio/ogg")


class IngestError(Exception):
    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status = status
        self.detail = detail


def object_ttl_s() -> float:
    try:
        return float(os.environ.get("OBJECT_TTL_S", "") or c.OBJECT_TTL_S)
    except ValueError:
        return float(c.OBJECT_TTL_S)


def now_utc() -> datetime:
    return datetime.now(UTC)


# ------------------------------------------------------------------------ bodies


class _Body(BaseModel):
    model_config = ConfigDict(extra="ignore")


class ObjectIn(_Body):
    object_id: str = Field(min_length=1, max_length=64)
    kind: c.ObjectKind
    callsign: c.Callsign | None = Field(default=None, max_length=16)
    label: str | None = Field(default=None, max_length=200)
    status: c.ObjectStatus
    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)
    # finite and physically plausible: NaN/Infinity or a negative speed would be stored and
    # then break JSON consumers (the visualizer) and the safety rules
    alt_ft: float | None = Field(default=None, ge=-2000, le=60000, allow_inf_nan=False)
    heading_deg: float | None = Field(default=None, ge=0, le=360, allow_inf_nan=False)
    speed_kt: float | None = Field(default=None, ge=0, le=1000, allow_inf_nan=False)


class PositionsIn(_Body):
    tower_id: str = c.TOWER_ID
    t: AwareDatetime | None = None
    objects: list[ObjectIn] = Field(max_length=MAX_OBJECTS)


class RemoveIn(_Body):
    tower_id: str = c.TOWER_ID
    reason: str | None = None


class UtteranceIn(_Body):
    tower_id: str = c.TOWER_ID
    stream_id: str = Field(min_length=1)
    t_start: AwareDatetime
    duration_s: float | None = Field(default=None, ge=0)
    speaker: str | None = None


class AudioMetaIn(UtteranceIn):
    truth_text: str | None = None


class TranscriptIn(UtteranceIn):
    text: str = Field(min_length=1)
    confidence: float | None = Field(default=None, ge=0, le=1)


class WeatherIn(_Body):
    tower_id: str = c.TOWER_ID
    t: AwareDatetime | None = None
    visibility_sm: float = Field(ge=0)
    wind: str | None = None
    raw: str | None = None


class StreamIn(_Body):
    id: str = Field(min_length=1)
    tower_id: str = c.TOWER_ID
    name: str = Field(min_length=1)
    kind: c.StreamKind
    frequency_mhz: float | None = None
    controller_id: str = Field(min_length=1)
    station: str = Field(min_length=1)


class RunIn(_Body):
    tower_id: str = c.TOWER_ID
    scenario: str = Field(min_length=1)
    kind: c.SimRunKind
    note: str | None = None


class ResetIn(_Body):
    tower_id: str = c.TOWER_ID
    scope: Literal["live"] = "live"


def parse(model: type[BaseModel], data: Any) -> Any:
    """Validate a body; 400 naming the first bad field."""
    if not isinstance(data, dict):
        raise IngestError(400, "body must be a JSON object")
    try:
        return model.model_validate(data)
    except ValidationError as e:
        err = e.errors()[0]
        field = ".".join(str(p) for p in err["loc"]) or "body"
        raise IngestError(400, f"{field}: {err['msg']}") from e


# ----------------------------------------------------------------------- checks


def _tower(db: Database, tower_id: str) -> None:
    if w.world(db, tower_id)["tower"] is None:
        raise IngestError(404, f"unknown tower_id {tower_id!r}")


def _when(t: datetime | None, now: datetime, field: str = "t") -> datetime:
    if t is None:
        return now
    t = t.astimezone(UTC)
    if abs((t - now).total_seconds()) > MAX_SKEW_S:
        raise IngestError(422, f"{field} is more than {MAX_SKEW_S:.0f} s from the tower clock")
    return t


def lane_for(db: Database, tower_id: str, o: ObjectIn) -> str | None:
    """The lane an object is on: airborne at/above 200 ft (or with no altitude) is on none."""
    if o.status == "airborne" and (o.alt_ft is None or o.alt_ft >= RUNWAY_AIRBORNE_MAX_FT):
        return None
    return w.lane_at(db, o.lon, o.lat, tower_id)


# -------------------------------------------------------------------- endpoints


def ingest_positions(db: Database, body: PositionsIn) -> dict:
    _tower(db, body.tower_id)
    now = now_utc()
    t = _when(body.t, now)
    expires = now + timedelta(seconds=object_ttl_s())
    ops, samples, lanes = [], [], {}
    for o in body.objects:
        lane_id = lane_for(db, body.tower_id, o)
        lanes[o.object_id] = lane_id
        point = {"type": "Point", "coordinates": [o.lon, o.lat]}
        ops.append(UpdateOne(
            {"_id": o.object_id},
            {"$set": {"tower_id": body.tower_id, "kind": o.kind, "callsign": o.callsign,
                      "label": o.label, "status": o.status, "position": point,
                      "alt_ft": o.alt_ft, "heading_deg": o.heading_deg,
                      "speed_kt": o.speed_kt, "lane_id": lane_id, "updated_at": now,
                      "expires_at": expires},
             "$setOnInsert": {"clearance": None, "hold_short": False}},
            upsert=True))
        samples.append({
            "t": t, "meta": {"object_id": o.object_id, "tower_id": body.tower_id,
                             "kind": o.kind, "callsign": o.callsign},
            "position": point, "alt_ft": o.alt_ft, "heading_deg": o.heading_deg,
            "speed_kt": o.speed_kt, "status": o.status, "lane_id": lane_id})
    if ops:
        db.objects.bulk_write(ops, ordered=False)
        db.positions.insert_many(samples, ordered=False)
    return {"accepted": len(ops), "lanes": lanes}


def remove_object(db: Database, object_id: str, body: RemoveIn) -> dict:
    _tower(db, body.tower_id)
    doc = db.objects.find_one_and_delete({"_id": object_id, "tower_id": body.tower_id})
    if doc is None:
        raise IngestError(404, f"unknown object_id {object_id!r}")
    log_state_event(db, "object_removed", object_id, {**doc, "reason": body.reason},
                    by="ingest", tower_id=body.tower_id)
    return {"removed": True}


def _stream(db: Database, tower_id: str, stream_id: str) -> dict:
    s = db.streams.find_one({"_id": stream_id, "tower_id": tower_id})  # fresh, not cached
    if s is None:
        raise IngestError(404, f"unknown stream_id {stream_id!r}")
    return s


def _clip(db: Database, meta: UtteranceIn, t_start: datetime, duration_s: float,
          now: datetime, **fields: Any) -> dict:
    doc = {
        "tower_id": meta.tower_id, "stream_id": meta.stream_id, "direction": "in",
        "t_start": t_start, "t_end": t_start + timedelta(seconds=duration_s),
        "duration_s": float(duration_s), "audio_id": None, "mime": None,
        "truth_text": None, "speaker": meta.speaker, "status": "queued",
        "transmission_id": None, "error": None, "created_at": now, **fields,
    }
    return {"clip_id": str(db.audio_clips.insert_one(doc).inserted_id)}


def ingest_audio(db: Database, meta: AudioMetaIn, data: bytes, mime: str | None,
                 filename: str | None = None) -> dict:
    if len(data) > MAX_AUDIO_BYTES:
        raise IngestError(413, f"audio is larger than {MAX_AUDIO_BYTES} bytes")
    if meta.duration_s is not None and meta.duration_s > MAX_AUDIO_S:
        raise IngestError(413, f"audio is longer than {MAX_AUDIO_S:.0f} s")
    if not data:
        raise IngestError(400, "file: empty")
    _tower(db, meta.tower_id)
    _stream(db, meta.tower_id, meta.stream_id)
    now = now_utc()
    t_start = _when(meta.t_start, now, "t_start")
    mime = (mime or "").split(";")[0].strip().lower() or None
    if mime and mime not in AUDIO_MIMES and mime != "application/octet-stream":
        raise IngestError(400, f"file: unsupported content type {mime!r}")
    audio_id = GridFSBucket(db, bucket_name=AUDIO_BUCKET).upload_from_stream(
        filename or f"{meta.stream_id}_{t_start:%Y%m%dT%H%M%S%f}", data,
        metadata={"stream_id": meta.stream_id, "tower_id": meta.tower_id,
                  "t_start": t_start, "direction": "in", "mime": mime})
    return _clip(db, meta, t_start, meta.duration_s or 0.0, now, audio_id=audio_id,
                 mime=mime, truth_text=meta.truth_text)


def ingest_transcript(db: Database, body: TranscriptIn) -> dict:
    _tower(db, body.tower_id)
    _stream(db, body.tower_id, body.stream_id)
    now = now_utc()
    t_start = _when(body.t_start, now, "t_start")
    duration = body.duration_s
    if duration is None:  # about 2.5 words per second of radio speech
        duration = round(max(1.0, len(body.text.split()) / 2.5), 1)
    if duration > MAX_AUDIO_S:
        raise IngestError(413, f"utterance is longer than {MAX_AUDIO_S:.0f} s")
    extra = {} if body.confidence is None else {"confidence": body.confidence}
    return _clip(db, body, t_start, duration, now, truth_text=body.text, **extra)


def ingest_weather(db: Database, body: WeatherIn) -> dict:
    _tower(db, body.tower_id)
    t = _when(body.t, now_utc())
    res = db.weather.insert_one({"t": t, "tower_id": body.tower_id,
                                 "visibility_sm": body.visibility_sm, "wind": body.wind,
                                 "raw": body.raw})
    return {"ok": True, "weather_id": str(res.inserted_id)}


def upsert_stream(db: Database, body: StreamIn) -> dict:
    _tower(db, body.tower_id)
    if db.controllers.find_one({"_id": body.controller_id, "tower_id": body.tower_id}) is None:
        raise IngestError(404, f"unknown controller_id {body.controller_id!r}")
    doc = c.to_mongo(c.Stream.model_validate({**body.model_dump(), "_id": body.id}))
    db.streams.replace_one({"_id": body.id}, doc, upsert=True)
    w.clear_cache()
    return {"ok": True, "stream_id": body.id}


def start_run(db: Database, body: RunIn) -> dict:
    _tower(db, body.tower_id)
    res = db.sim_runs.insert_one({"tower_id": body.tower_id, "scenario": body.scenario,
                                  "kind": body.kind, "note": body.note,
                                  "started_at": now_utc(), "ended_at": None})
    return {"run_id": str(res.inserted_id)}


def end_run(db: Database, run_id: str) -> dict:
    if not ObjectId.is_valid(run_id):
        raise IngestError(404, f"unknown run_id {run_id!r}")
    res = db.sim_runs.update_one({"_id": ObjectId(run_id)}, {"$set": {"ended_at": now_utc()}})
    if res.matched_count == 0:
        raise IngestError(404, f"unknown run_id {run_id!r}")
    return {"ok": True, "run_id": run_id}


def reset_live(db: Database, body: ResetIn) -> dict:
    _tower(db, body.tower_id)
    now = now_utc()
    n_obj = db.objects.delete_many({"tower_id": body.tower_id}).deleted_count
    n_ctx = db.context.delete_many({"tower_id": body.tower_id}).deleted_count
    cleared = []
    for ls in db.lane_status.find({"tower_id": body.tower_id, "state": {"$ne": "clear"}}):
        doc = {**ls, "state": "clear", "occupants": [], "reservations": [], "reason": None,
               "updated_at": now}
        db.lane_status.replace_one({"_id": ls["_id"]}, doc)
        log_state_event(db, "lane_status", ls["_id"], doc, by="ingest_reset",
                        tower_id=body.tower_id, t=now)
        cleared.append(ls["_id"])
    return {"ok": True, "objects_removed": n_obj, "context_removed": n_ctx,
            "lanes_cleared": cleared}


def world_doc(db: Database, tower_id: str) -> dict:
    _tower(db, tower_id)
    return w.world(db, tower_id)


__all__ = [
    "AudioMetaIn", "IngestError", "PositionsIn", "RemoveIn", "ResetIn", "RunIn", "StreamIn",
    "TranscriptIn", "WeatherIn", "end_run", "ingest_audio", "ingest_positions",
    "ingest_transcript", "ingest_weather", "lane_for", "parse", "remove_object",
    "reset_live", "start_run", "upsert_stream", "world_doc",
]
