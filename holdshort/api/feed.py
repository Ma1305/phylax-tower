"""JSON-safe documents and one shared change stream fanned out to WebSocket clients.

Events have the shape ``{"type", "doc", "collection", "op"}``:

- ``type``: transmission | decision | permission | state (flight_state and runway_status)
- ``doc``: the full document (after the change), JSON-safe; for deletes (flight_state TTL)
  only ``{"_id": ...}``
- ``collection``: source collection; ``op``: insert | update | replace | delete
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from datetime import UTC, date, datetime
from typing import Any

from bson import ObjectId
from pymongo.database import Database
from pymongo.errors import PyMongoError

from holdshort.env import redact

log = logging.getLogger("holdshort.api.feed")

EVENT_TYPES: dict[str, str] = {
    "transmissions": "transmission",
    "decisions": "decision",
    "permissions": "permission",
    "flight_state": "state",
    "runway_status": "state",
}
RECENT_LIMIT = 50


def iso(dt: datetime) -> str:
    """ISO 8601 in UTC with a Z suffix (naive datetimes are taken as UTC)."""
    dt = dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)
    spec = "milliseconds" if dt.microsecond else "seconds"
    return dt.isoformat(timespec=spec).replace("+00:00", "Z")


def jsonable(obj: Any) -> Any:
    """Recursively convert ObjectId -> str and datetime -> ISO 8601 Z."""
    if isinstance(obj, dict):
        return {str(k): jsonable(v) for k, v in obj.items()}
    if isinstance(obj, list | tuple):
        return [jsonable(v) for v in obj]
    if isinstance(obj, ObjectId):
        return str(obj)
    if isinstance(obj, datetime):
        return iso(obj)
    if isinstance(obj, date):
        return obj.isoformat()
    return obj


def _recent(db: Database, coll: str) -> list[dict]:
    docs = list(db[coll].find().sort([("t", -1), ("_id", -1)]).limit(RECENT_LIMIT))
    docs.reverse()  # oldest first, ready to render in order
    return docs


def snapshot(db: Database) -> dict:
    """The current picture a newly connected client needs (JSON-safe)."""
    return jsonable(
        {
            "permissions": list(db.permissions.find().sort("agent", 1)),
            "rules": list(db.rules.find().sort("version", 1)),
            "transmissions": _recent(db, "transmissions"),
            "decisions": _recent(db, "decisions"),
            "flight_state": list(db.flight_state.find().sort("callsign", 1)),
            "runway_status": list(db.runway_status.find()),
        }
    )


def to_event(change: dict) -> dict | None:
    """Turn a change stream event into a WebSocket event (None if not relevant)."""
    coll = change.get("ns", {}).get("coll")
    kind = EVENT_TYPES.get(coll)
    if kind is None:
        return None
    op = change.get("operationType")
    doc = change.get("fullDocument")
    if doc is None:  # delete, or an update whose document is already gone
        doc = dict(change.get("documentKey") or {})
        if op != "delete":
            op = "delete"
    return {"type": kind, "doc": jsonable(doc), "collection": coll, "op": op}


class ChangeFeed:
    """One change stream on the watched collections; each subscriber gets every event.

    The stream runs in a daemon thread; events are handed to each subscriber's
    asyncio.Queue on that subscriber's event loop. On a stream error it reopens
    from the last resume token.
    """

    PIPELINE = [
        {
            "$match": {
                "ns.coll": {"$in": list(EVENT_TYPES)},
                "operationType": {"$in": ["insert", "update", "replace", "delete"]},
            }
        }
    ]

    def __init__(self, db: Database, max_await_ms: int = 300):
        self.db = db
        self.max_await_ms = max_await_ms
        self.ready = threading.Event()
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._subs: set[tuple[asyncio.AbstractEventLoop, asyncio.Queue]] = set()
        self._thread: threading.Thread | None = None

    # ---------------------------------------------------------------- subscribers
    def subscribe(self) -> asyncio.Queue:
        """Call from a coroutine. Returns the queue that will receive events."""
        q: asyncio.Queue = asyncio.Queue()
        with self._lock:
            self._subs.add((asyncio.get_running_loop(), q))
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        with self._lock:
            self._subs = {s for s in self._subs if s[1] is not q}

    def publish(self, event: dict) -> None:
        with self._lock:
            subs = list(self._subs)
        for loop, q in subs:
            try:
                loop.call_soon_threadsafe(q.put_nowait, event)
            except RuntimeError:  # loop closed
                self.unsubscribe(q)

    # -------------------------------------------------------------------- thread
    def start(self) -> None:
        self._thread = threading.Thread(target=self.run, name="change-feed", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout)

    def run(self) -> None:
        token = None
        while not self._stop.is_set():
            try:
                with self.db.watch(
                    self.PIPELINE,
                    full_document="updateLookup",
                    resume_after=token,
                    max_await_time_ms=self.max_await_ms,
                ) as stream:
                    self.ready.set()
                    while not self._stop.is_set():
                        change = stream.try_next()
                        token = stream.resume_token
                        if change is None:
                            continue
                        event = to_event(change)
                        if event is not None:
                            self.publish(event)
            except PyMongoError as e:
                log.warning("change feed error, reopening in 1s: %s", redact(e))
                time.sleep(1)
        log.info("change feed stopped")
