"""A stand-in for the tower's ``/ingest/*`` API, for building the simulator before the tower exists.

It accepts every call in ``docs/v2/SIMULATOR_CONTRACT.md``, checks each one the way the
contract says the tower does (400, 401, 404, 413, 422) and keeps what it was sent in
memory, at ``app.state.store``, so tests can look at it. Audio is measured and dropped.

Outside the contract, for developers:

- ``GET /health``, ``GET /stub/state`` (what the stub holds) and ``POST /stub/outbound``
  (make the stub tell traffic something, to try the simulator's reaction by hand);
- one built-in safety rule, on only when ``auto_stop_after_s`` is given: when a controller
  clears someone to cross a runway that another callsign is cleared to land on, the stub
  tells the crosser to stop that many seconds later. It stands in for the tower's agents
  so the closed loop can be tested.

Serve it with ``python -m hssim stub --port 8000 --auto-stop 3`` (``hssim/cli.py``), or hand
``create_stub_app()`` to uvicorn or to an in-process test client.
"""

from __future__ import annotations

import json
import math
import os
import re
import threading
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, Depends, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.datastructures import UploadFile

from hssim import phrases
from hssim.world import World

MAX_OBJECTS = 500
MAX_AUDIO_BYTES = 5 * 1024 * 1024
MAX_DURATION_S = 60.0
MAX_CLOCK_SKEW_S = 120.0
OBJECT_TTL_S = 120.0
AUDIO_TYPES = ("audio/wav", "audio/mpeg", "audio/ogg")
OBJECT_KINDS = ("aircraft", "vehicle", "person", "unknown")
OBJECT_STATUSES = ("airborne", "ground")
RUN_KINDS = ("normal", "incident")
OUTBOUND_ACTIONS = ("stop", "propose_instruction")
CONTROLLER_STATIONS = ("TOWER", "GROUND", "OPS")

# "runway four", "cross runway one three", "runway 04": the digits after the word "runway"
_RUNWAY = re.compile(rf"\b(cross )?runway ((?:(?:{'|'.join(phrases.DIGITS)}|\d+)\b ?)+)")


# ---------------------------------------------------------------------------- time
def _floor_ms(t: datetime) -> datetime:
    return t.replace(microsecond=t.microsecond // 1000 * 1000)


def _iso(t: datetime) -> str:
    """``2026-11-14T19:05:12.250Z``"""
    return t.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _parse_iso(value: Any) -> datetime:
    """An ISO 8601 time with ``Z`` or an offset, as UTC. Raises ValueError for anything else."""
    if not isinstance(value, str):
        raise ValueError("not a string")
    t = datetime.fromisoformat(value)
    if t.tzinfo is None:
        raise ValueError("no time zone")
    return t.astimezone(UTC)


# --------------------------------------------------------------------------- store
class StubStore:
    """What the stub tower has been sent and what it has told traffic, all in memory.

    Nothing runs in the background. An object not reported for 120 s is dropped the next
    time ``objects`` is read, and a call scheduled by the safety rule joins ``outbound``
    the next time that is read, stamped with the time it was due.
    """

    def __init__(self, world: World | None = None, *, auto_stop_after_s: float | None = None):
        self.world = world or World.load_default()
        self.auto_stop_after_s = auto_stop_after_s
        self.position_batches: list[dict] = []
        self.clips: list[dict] = []
        self.weather: list[dict] = []
        self.runs: list[dict] = []
        self.resets: list[dict] = []
        self.removed: list[dict] = []
        self.streams: dict[str, dict] = {sid: dict(s) for sid, s in self.world.streams.items()}
        self._objects: dict[str, dict] = {}
        self._last_seen: dict[str, datetime] = {}
        self._outbound: list[dict] = []
        self._scheduled: list[tuple[datetime, dict]] = []  # (due time, add_outbound arguments)
        self._last_poll: datetime | None = None  # the newest "now" GET /ingest/outbound gave out
        self._callsigns: dict[str, None] = {}  # every callsign reported so far, in order
        self._landing: dict[str, str] = {}  # callsign cleared to land -> runway lane id
        # how a runway is named on the radio ("four", "zero four", "04", "4") -> (designator, lane id)
        self._runways: dict[str, tuple[str, str]] = {}
        for lane in self.world.lanes.values():
            for d in lane.designators if lane.kind == "runway" else []:
                if d.isdigit():
                    for name in (phrases.spoken_runway(d), phrases.digits(d), d, str(int(d))):
                        self._runways[name] = (d, lane.id)
        # TestClient runs each request on its own event loop thread unless used as a context
        # manager, so requests sent from several threads really do run at the same time
        self._lock = threading.RLock()

    def now(self) -> datetime:
        """The stub's clock. Tests replace it to move time."""
        return datetime.now(UTC)

    # --------------------------------------------------------------------- objects
    @property
    def objects(self) -> dict[str, dict]:
        """The current object per ``object_id``: its last reported fields plus ``lane_id`` and ``t``."""
        cutoff = self.now() - timedelta(seconds=OBJECT_TTL_S)
        with self._lock:
            for object_id in [o for o, seen in self._last_seen.items() if seen < cutoff]:
                del self._last_seen[object_id]
                self._objects.pop(object_id, None)
            return self._objects

    def report_positions(self, body: dict, t: datetime) -> dict[str, str | None]:
        """Take one validated ``POST /ingest/positions`` body; returns the lane of each object."""
        now = self.now()
        lanes: dict[str, str | None] = {}
        with self._lock:
            objects = self.objects
            for obj in body["objects"]:
                object_id = obj["object_id"]
                lane = self.world.lane_at(obj["lon"], obj["lat"], obj["status"], obj.get("alt_ft"))
                objects[object_id] = {**obj, "lane_id": lane, "t": _iso(t)}
                self._last_seen[object_id] = now
                if obj.get("callsign"):
                    self._callsigns[obj["callsign"]] = None
                lanes[object_id] = lane
            # the body as it was sent; a missing "t" is filled in with the time that was used
            self.position_batches.append({**body, "t": body.get("t") or _iso(t),
                                          "received_at": _iso(now)})
        return lanes

    def remove_object(self, object_id: str, tower_id: str, reason: str | None) -> bool:
        """Drop an object at once. False when there is no such object."""
        with self._lock:
            if self.objects.pop(object_id, None) is None:
                return False
            self._last_seen.pop(object_id, None)
            self.removed.append({"object_id": object_id, "tower_id": tower_id, "reason": reason,
                                 "received_at": _iso(self.now())})
            return True

    def reset(self, tower_id: str, scope: str) -> dict:
        """Clear live state: the objects and the safety rule's memory. The logs are kept."""
        with self._lock:
            self._promote_due()  # a call that was already due belongs to the log
            record = {"tower_id": tower_id, "scope": scope, "objects_cleared": len(self.objects),
                      "received_at": _iso(self.now())}
            self._objects.clear()
            self._last_seen.clear()
            self._landing.clear()
            self._scheduled.clear()
            self.resets.append(record)
            return record

    # ----------------------------------------------------------------------- clips
    def add_clip(self, *, kind: str, stream_id: str, t_start: datetime, duration_s: float | None,
                 speaker: str | None, text: str | None, size_bytes: int | None = None,
                 mime: str | None = None) -> dict:
        """Record one utterance (``kind`` is "audio" or "transcript"); the safety rule hears it."""
        now = self.now()
        clip = {"clip_id": uuid.uuid4().hex, "kind": kind, "stream_id": stream_id,
                "t_start": _iso(t_start), "duration_s": duration_s, "speaker": speaker,
                "text": text, "size_bytes": size_bytes, "mime": mime, "received_at": _iso(now)}
        with self._lock:
            self.clips.append(clip)
            self._hear(clip, now)
        return clip

    def _hear(self, clip: dict, now: datetime) -> None:
        """The safety rule, fed every utterance that comes with its text.

        A controller's "cleared to land" puts the callsign it names on that runway until that
        aircraft says "clear of runway" or "vacated" (or a reset). A controller's "cross runway
        N" to anyone else while a landing is on that runway schedules a stop for them.
        """
        if self.auto_stop_after_s is None or not clip["text"]:
            return
        text = re.sub(r"[^a-z0-9]+", " ", clip["text"].lower()).strip()
        named = phrases.find_callsigns(clip["text"], list(self._callsigns))
        speaker = (clip["speaker"] or "").upper()
        if speaker not in CONTROLLER_STATIONS:
            if "clear of runway" in text or "vacated" in text:
                # said by the speaker, or by whoever the text names when the speaker is no callsign
                who = [c for c in self._callsigns if c.upper() == speaker] or named
                for callsign in who:
                    self._landing.pop(callsign, None)
            return
        runways = self._runways_named(text)
        cleared_at = text.find("cleared to land")
        if cleared_at >= 0 and named and runways:
            before = [r for r in runways if r[0] < cleared_at]
            self._landing[named[0]] = (before[-1] if before else runways[0])[3]
        for _, crossing, designator, lane_id in runways:
            landing = [c for c, lane in self._landing.items() if lane == lane_id]
            target = next((c for c in named if c not in landing), None)
            if crossing and landing and target:
                spoken = phrases.spoken_callsign(target)
                self._scheduled.append((now + timedelta(seconds=self.auto_stop_after_s), {
                    "action": "stop", "target": [target], "lane_id": lane_id,
                    "stream_id": clip["stream_id"],
                    "text": f"{spoken}, STOP. Hold short runway {phrases.digits(designator)}."}))

    def _runways_named(self, text: str) -> list[tuple[int, bool, str, str]]:
        """Each runway named in ``text`` (lower case, no punctuation), in order:
        (where, whether it was "cross runway ...", designator, lane id)."""
        found = []
        for m in _RUNWAY.finditer(text):
            words = m.group(2).split()
            # "runway one three" is 13; in "runway four, one six zero at eight" it is only "four"
            name = next((n for n in (" ".join(words[:2]), words[0]) if n in self._runways), None)
            if name:
                found.append((m.start(), bool(m.group(1)), *self._runways[name]))
        return found

    # -------------------------------------------------------------------- outbound
    @property
    def outbound(self) -> list[dict]:
        """The calls ``GET /ingest/outbound`` returns, oldest first."""
        self._promote_due()
        return self._outbound

    def add_outbound(self, *, action: str = "stop", target: list[str], text: str,
                     lane_id: str | None = None, stream_id: str | None = None,
                     approved_by: str = "stub") -> dict:
        """Tell traffic something now; returns the call as ``GET /ingest/outbound`` gives it."""
        with self._lock:
            self._promote_due()
            return self._emit(self.now(), action=action, target=target, text=text,
                              lane_id=lane_id, stream_id=stream_id, approved_by=approved_by)

    def outbound_since(self, since: datetime | None) -> tuple[datetime, list[dict]]:
        """``(now, the calls made after since)``; every call when ``since`` is None.

        A caller that passes each ``now`` back as its next ``since`` gets every call exactly
        once: ``now`` is never before a call already made, and ``_emit`` stamps every later
        call after it.
        """
        with self._lock:
            calls = self.outbound
            times = [_parse_iso(c["t"]) for c in calls]
            now = max([_floor_ms(self.now()), *times])
            if self._last_poll is not None:
                now = max(now, self._last_poll)  # the clock may step back; the answers must not
            self._last_poll = now
            return now, [c for c, t in zip(calls, times) if since is None or t > since]

    def _promote_due(self) -> None:
        now = self.now()
        with self._lock:
            due = sorted((s for s in self._scheduled if s[0] <= now), key=lambda s: s[0])
            self._scheduled = [s for s in self._scheduled if s[0] > now]
            for when, call in due:
                self._emit(when, **call)

    def _emit(self, when: datetime, *, action: str, target: list[str], text: str,
              lane_id: str | None, stream_id: str | None, approved_by: str = "stub") -> dict:
        t = _floor_ms(when)
        if self._last_poll is not None and t <= self._last_poll:
            t = self._last_poll + timedelta(milliseconds=1)
        call = {"t": _iso(t), "decision_id": uuid.uuid4().hex, "stream_id": stream_id,
                "action": action, "target": list(target), "lane_id": lane_id, "text": text,
                "approved_by": approved_by, "clip_id": uuid.uuid4().hex}
        self._outbound.append(call)
        return call


# ---------------------------------------------------------- checking what was sent
class ApiError(Exception):
    """An error answer: the status code, the field at fault (if one is) and what is wrong with it."""

    def __init__(self, status: int, field: str | None, problem: str):
        super().__init__(problem)
        self.status = status
        self.field = field
        self.detail = f"{field}: {problem}" if field else problem


async def _error_response(_: Request, exc: ApiError) -> JSONResponse:
    return JSONResponse({"detail": exc.detail, "field": exc.field}, status_code=exc.status)


def _store(request: Request) -> StubStore:
    return request.app.state.store


def _require_key(request: Request) -> None:
    key = request.app.state.ingest_key
    if key and request.headers.get("x-ingest-key") != key:
        raise ApiError(401, "X-Ingest-Key", "header is missing or wrong")


async def _body(request: Request, *, optional: bool = False) -> dict:
    raw = await request.body()
    if optional and not raw.strip():
        return {}
    try:
        body = json.loads(raw)
    except ValueError:
        body = None
    if not isinstance(body, dict):
        raise ApiError(400, "body", "must be a JSON object")
    return body


def _text(src: dict, name: str, *, at: str = "", required: bool = True) -> str | None:
    value = src.get(name)
    if value is None and not required:
        return None
    if not isinstance(value, str) or (required and not value.strip()):
        raise ApiError(400, at + name, "must be a non-empty string" if required else "must be a string")
    return value


def _number(src: dict, name: str, *, at: str = "", required: bool = True,
            lo: float | None = None, hi: float | None = None) -> float | None:
    value = src.get(name)
    if value is None and not required:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ApiError(400, at + name, "must be a number")
    if (lo is not None and value < lo) or (hi is not None and value > hi):
        limits = f"between {lo} and {hi}" if hi is not None else f"at least {lo}"
        raise ApiError(400, at + name, f"must be {limits}")
    return value


def _choice(src: dict, name: str, allowed: tuple[str, ...], *, at: str = "") -> str:
    value = src.get(name)
    if not isinstance(value, str) or value not in allowed:
        raise ApiError(400, at + name, f"must be one of {', '.join(allowed)}")
    return value


def _time(src: dict, name: str, *, at: str = "", default: datetime | None = None) -> datetime:
    """The time in ``src[name]``; ``default`` (when given) if it is missing."""
    if src.get(name) is None and default is not None:
        return default
    try:
        return _parse_iso(src.get(name))
    except ValueError:
        raise ApiError(400, at + name, "must be an ISO 8601 UTC time ending in Z, "
                                       "like 2026-11-14T19:05:12.250Z") from None


def _check_clock(t: datetime, now: datetime, field: str) -> None:
    off = abs((t - now).total_seconds())
    if off > MAX_CLOCK_SKEW_S:
        raise ApiError(422, field, f"is {off:.0f} s from the tower's clock ({_iso(now)}); "
                                   f"at most {MAX_CLOCK_SKEW_S:.0f} s is accepted")


def _tower(store: StubStore, value: Any, field: str = "tower_id") -> str:
    if not isinstance(value, str) or not value:
        raise ApiError(400, field, "is required")
    if value != store.world.tower_id:
        raise ApiError(404, field, f"unknown tower {value!r}")
    return value


def _check_object(obj: Any, at: str) -> None:
    if not isinstance(obj, dict):
        raise ApiError(400, at, "must be an object")
    at += "."
    _text(obj, "object_id", at=at)
    _choice(obj, "kind", OBJECT_KINDS, at=at)
    _choice(obj, "status", OBJECT_STATUSES, at=at)
    _number(obj, "lat", at=at, lo=-90, hi=90)
    _number(obj, "lon", at=at, lo=-180, hi=180)
    for name in ("callsign", "label"):
        _text(obj, name, at=at, required=False)
    for name in ("alt_ft", "heading_deg", "speed_kt"):
        _number(obj, name, at=at, required=False)


def _utterance(store: StubStore, src: dict, *, at: str = "",
               need_duration: bool) -> tuple[str, datetime, float | None, str | None]:
    """The fields audio ``meta`` and a transcript share: 400 for a bad one, 404 for an unknown id."""
    _tower(store, src.get("tower_id"), at + "tower_id")
    stream_id = _text(src, "stream_id", at=at)
    t_start = _time(src, "t_start", at=at)
    duration_s = _number(src, "duration_s", at=at, required=need_duration, lo=0)
    speaker = _text(src, "speaker", at=at, required=False)
    if stream_id not in store.streams:
        raise ApiError(404, at + "stream_id", f"unknown stream {stream_id!r}")
    return stream_id, t_start, duration_s, speaker


def _check_utterance_limits(store: StubStore, t_start: datetime, duration_s: float | None,
                            at: str = "") -> None:
    if duration_s is not None and duration_s > MAX_DURATION_S:
        raise ApiError(413, at + "duration_s", f"an utterance is at most {MAX_DURATION_S:.0f} s")
    _check_clock(t_start, store.now(), at + "t_start")


# ------------------------------------------------------------- the contract's API
ingest = APIRouter(prefix="/ingest", dependencies=[Depends(_require_key)])


@ingest.get("/world")
async def get_world(request: Request) -> dict[str, Any]:
    store = _store(request)
    _tower(store, request.query_params.get("tower_id"))
    world = store.world.as_ingest_world()
    world["streams"] = list(store.streams.values())
    return world


@ingest.post("/positions")
async def post_positions(request: Request) -> dict[str, Any]:
    store = _store(request)
    body = await _body(request)
    _tower(store, body.get("tower_id"))
    now = store.now()
    t = _time(body, "t", default=now)
    objects = body.get("objects")
    if not isinstance(objects, list):
        raise ApiError(400, "objects", "must be a list")
    if len(objects) > MAX_OBJECTS:
        raise ApiError(400, "objects", f"has {len(objects)} objects; at most {MAX_OBJECTS} per call")
    for i, obj in enumerate(objects):
        _check_object(obj, f"objects[{i}]")
    _check_clock(t, now, "t")
    return {"accepted": len(objects), "lanes": store.report_positions(body, t)}


@ingest.post("/objects/{object_id}/remove")
async def remove_object(object_id: str, request: Request) -> dict[str, Any]:
    store = _store(request)
    body = await _body(request)
    tower_id = _tower(store, body.get("tower_id"))
    reason = _text(body, "reason", required=False)
    if not store.remove_object(object_id, tower_id, reason):
        raise ApiError(404, "object_id", f"unknown object {object_id!r}")
    return {"removed": True}


@ingest.post("/audio", status_code=202)
async def post_audio(request: Request) -> dict[str, Any]:
    store = _store(request)
    async with request.form() as form:
        raw, upload = form.get("meta"), form.get("file")
        if not isinstance(raw, str):
            raise ApiError(400, "meta", "is required: a form field holding a JSON string")
        try:
            meta = json.loads(raw)
        except ValueError:
            meta = None
        if not isinstance(meta, dict):
            raise ApiError(400, "meta", "must be a JSON object")
        if not isinstance(upload, UploadFile):
            raise ApiError(400, "file", "is required: the audio, as a file part")
        stream_id, t_start, duration_s, speaker = _utterance(store, meta, at="meta.",
                                                             need_duration=True)
        truth_text = _text(meta, "truth_text", at="meta.", required=False)
        mime = (upload.content_type or "").split(";")[0].strip().lower()
        if mime not in AUDIO_TYPES:
            raise ApiError(400, "file", f"content type {mime!r} is not one of {', '.join(AUDIO_TYPES)}")
        size = upload.file.seek(0, os.SEEK_END)  # measured where the upload was spooled, never read
        if size == 0:
            raise ApiError(400, "file", "is empty")
        if size > MAX_AUDIO_BYTES:
            raise ApiError(413, "file", f"is {size} bytes; at most {MAX_AUDIO_BYTES} (5 MB)")
        _check_utterance_limits(store, t_start, duration_s, at="meta.")
    clip = store.add_clip(kind="audio", stream_id=stream_id, t_start=t_start, duration_s=duration_s,
                          speaker=speaker, text=truth_text, size_bytes=size, mime=mime)
    return {"clip_id": clip["clip_id"]}


@ingest.post("/transcripts", status_code=202)
async def post_transcript(request: Request) -> dict[str, Any]:
    store = _store(request)
    body = await _body(request)
    stream_id, t_start, duration_s, speaker = _utterance(store, body, need_duration=False)
    text = _text(body, "text")
    _number(body, "confidence", required=False, lo=0, hi=1)
    _check_utterance_limits(store, t_start, duration_s)
    clip = store.add_clip(kind="transcript", stream_id=stream_id, t_start=t_start,
                          duration_s=duration_s, speaker=speaker, text=text)
    return {"clip_id": clip["clip_id"]}


@ingest.post("/weather")
async def post_weather(request: Request) -> dict[str, Any]:
    store = _store(request)
    body = await _body(request)
    _tower(store, body.get("tower_id"))
    now = store.now()
    t = _time(body, "t", default=now)
    _number(body, "visibility_sm", lo=0)
    _text(body, "wind")
    _text(body, "raw", required=False)
    _check_clock(t, now, "t")
    store.weather.append({**body, "t": body.get("t") or _iso(t), "received_at": _iso(now)})
    return {"ok": True}


@ingest.post("/streams")
async def post_stream(request: Request) -> dict[str, Any]:
    store = _store(request)
    body = await _body(request)
    _tower(store, body.get("tower_id"))
    stream_id = _text(body, "id")
    fields = {name: _text(body, name, required=False)
              for name in ("name", "kind", "controller_id", "station")}
    fields["frequency_mhz"] = _number(body, "frequency_mhz", required=False, lo=0)
    # kept in the world's own shape, because GET /ingest/world lists them
    store.streams[stream_id] = {**store.streams.get(stream_id, {"_id": stream_id}),
                                **{k: v for k, v in fields.items() if v is not None}}
    return {"ok": True}


@ingest.post("/runs")
async def start_run(request: Request) -> dict[str, Any]:
    store = _store(request)
    body = await _body(request)
    _tower(store, body.get("tower_id"))
    run = {"run_id": uuid.uuid4().hex, "scenario": _text(body, "scenario"),
           "kind": _choice(body, "kind", RUN_KINDS), "note": _text(body, "note", required=False),
           "started_at": _iso(store.now()), "ended_at": None}
    store.runs.append(run)
    return {"run_id": run["run_id"]}


@ingest.post("/runs/{run_id}/end")
async def end_run(run_id: str, request: Request) -> dict[str, Any]:
    store = _store(request)
    body = await _body(request, optional=True)  # the contract gives this call no body
    if body.get("tower_id") is not None:
        _tower(store, body["tower_id"])
    run = next((r for r in store.runs if r["run_id"] == run_id), None)
    if run is None:
        raise ApiError(404, "run_id", f"unknown run {run_id!r}")
    run["ended_at"] = run["ended_at"] or _iso(store.now())
    return {"ok": True}


@ingest.post("/reset")
async def post_reset(request: Request) -> dict[str, Any]:
    store = _store(request)
    body = await _body(request)
    tower_id = _tower(store, body.get("tower_id"))
    store.reset(tower_id, _choice(body, "scope", ("live",)))
    return {"ok": True}


@ingest.get("/outbound")
async def get_outbound(request: Request) -> dict[str, Any]:
    store = _store(request)
    _tower(store, request.query_params.get("tower_id"))
    since = None
    if request.query_params.get("since"):
        # an unescaped "+00:00" in a query string arrives as " 00:00"
        since = _time({"since": request.query_params["since"].replace(" ", "+")}, "since")
    now, calls = store.outbound_since(since)
    return {"now": _iso(now), "calls": calls}


# ------------------------------------------------------------- developer helpers
dev = APIRouter()


@dev.get("/health")
async def health() -> dict[str, Any]:
    return {"status": "ok", "stub": True}


@dev.get("/stub/state")
async def stub_state(request: Request) -> dict[str, Any]:
    store = _store(request)
    objects, outbound = store.objects, store.outbound
    counts = {"objects": len(objects), "position_batches": len(store.position_batches),
              "clips": len(store.clips), "weather": len(store.weather), "runs": len(store.runs),
              "resets": len(store.resets), "removed": len(store.removed),
              "streams": len(store.streams), "outbound": len(outbound)}
    return {"now": _iso(store.now()), "counts": counts, "clips": store.clips[-20:],
            "objects": objects, "outbound": outbound}


@dev.post("/stub/outbound")
async def inject_outbound(request: Request) -> dict[str, Any]:
    store = _store(request)
    body = await _body(request)
    target = body.get("target")
    if not isinstance(target, list) or not all(isinstance(x, str) and x for x in target):
        raise ApiError(400, "target", 'must be a list of object ids, like ["TRUCK1"] '
                                      "([] for a call to nobody in particular)")
    return store.add_outbound(
        action=_choice({"action": body.get("action", "stop")}, "action", OUTBOUND_ACTIONS),
        target=target, text=_text(body, "text"), lane_id=_text(body, "lane_id", required=False),
        stream_id=_text(body, "stream_id", required=False))


# ------------------------------------------------------------------------- the app
def create_stub_app(world: World | None = None, *, ingest_key: str | None = None,
                    auto_stop_after_s: float | None = None) -> FastAPI:
    """The stub tower. ``app.state.store`` is its :class:`StubStore`.

    ``ingest_key``: when set, every ``/ingest/*`` call must send it as ``X-Ingest-Key``.
    ``auto_stop_after_s``: when set, turns on the safety rule (see the module docstring).
    """
    app = FastAPI(title="Hold Short stub tower")
    app.state.store = StubStore(world, auto_stop_after_s=auto_stop_after_s)
    app.state.ingest_key = ingest_key
    app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
    app.add_exception_handler(ApiError, _error_response)
    app.include_router(ingest)
    app.include_router(dev)
    return app
