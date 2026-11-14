"""Thin worker: change streams on objects / transmissions / lane_status -> StateEngine ->
writes to objects (clearance, hold_short), lane_status and state_events.

Events are applied to the engine as they arrive; the engine is flushed when the stream goes
quiet or at least every ``FLUSH_S`` seconds, so a burst of 1 Hz position updates leads to at
most one lane_status write per lane. On start everything is recomputed once from the stored
collections (and callsign-only reservations from the last 5 minutes of transmissions).
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import Any

from pymongo.database import Database

from holdshort import world
from holdshort.agents._stream import StreamAgent
from holdshort.events import log_state_event
from holdshort.state.core import CALLSIGN_HOLD, StateEngine, Writes

FLUSH_S = 1.0


def _now() -> datetime:
    return datetime.now(UTC)


class StateWorker(StreamAgent):
    name = "state"
    watch_collections = ["objects", "transmissions", "lane_status"]
    operation_types = ("insert", "update", "replace", "delete")
    store_token = False  # we recompute everything on start instead of replaying

    def __init__(self, db: Database, max_await_ms: int = 300, tower_id: str = "LGA"):
        super().__init__(db, max_await_ms=max_await_ms)
        self.tower_id = tower_id
        self.engine = self.build_engine()
        self.last_flush = time.monotonic()

    # ----------------------------------------------------------------- setup

    def build_engine(self) -> StateEngine:
        w = world.world(self.db, self.tower_id)
        lanes = w["lanes"]
        owners = {lane["_id"]: world.owner_of(self.db, lane["_id"]) for lane in lanes}

        def resolve(tx: dict) -> str | None:
            if tx.get("lane_id"):
                return tx["lane_id"]
            return world.lane_for_designator(self.db, tx.get("tower_id") or self.tower_id,
                                             tx.get("runway"))

        return StateEngine(lanes, owners, resolve)

    def recompute(self, now: datetime | None = None) -> Writes:
        now = now or _now()
        e = self.engine
        for doc in self.db.lane_status.find({"tower_id": self.tower_id}):
            e.load_status(doc)
        for doc in self.db.objects.find({"tower_id": self.tower_id}):
            e.objects[doc["_id"]] = dict(doc)
            e._track(doc["_id"], now)
        known = {o.get("callsign") or oid for oid, o in e.objects.items()} | set(e.objects)
        for tx in self.db.transmissions.find(
                {"tower_id": self.tower_id, "t": {"$gte": now - CALLSIGN_HOLD}}).sort("t", 1):
            target = tx.get("from") if tx.get("kind") == "report" else tx.get("to")
            if target not in known:
                e.on_transmission(tx, now)
        e.recompute_all()
        return self.flush(now)

    def on_ready(self) -> None:
        self.recompute()

    # ---------------------------------------------------------------- events

    def handle(self, doc: dict[str, Any], coll: str | None = None) -> None:
        now = _now()
        if coll == "transmissions":
            self.engine.on_transmission(doc, now)
        elif coll == "objects":
            if set(doc) <= {"_id"}:          # delete (or gone before the lookup)
                self.engine.on_object_removed(doc["_id"])
            elif doc.get("tower_id") == self.tower_id:
                self.engine.on_object(doc, now)
        elif coll == "lane_status" and set(doc) - {"_id"}:
            self.engine.load_status(doc)

    def flush(self, now: datetime | None = None) -> Writes:
        now = now or _now()
        writes = self.engine.flush(now)
        self.apply(writes)
        self.last_flush = time.monotonic()
        return writes

    def apply(self, writes: Writes) -> None:
        for oid, fields in writes.objects.items():
            self.db.objects.update_one({"_id": oid}, {"$set": fields})
        for lane_id, doc in writes.lanes.items():
            self.db.lane_status.replace_one({"_id": lane_id}, doc, upsert=True)
            log_state_event(self.db, "lane_status", lane_id, doc, by="state",
                            tower_id=doc.get("tower_id") or self.tower_id,
                            t=doc["updated_at"])
            self.log.info("lane %s -> %s", lane_id, doc["state"])

    # ------------------------------------------------------------------ loop

    def run(self) -> None:
        stream = self._open()
        try:
            self.on_ready()
            self.ready.set()
            self.log.info("READY %s watching %s.%s", self.name, self.db.name,
                          ",".join(self.collections))
            while not self._stop.is_set():
                change = stream.try_next()
                if change is not None:
                    self._safe_process(change)
                if change is None or time.monotonic() - self.last_flush >= FLUSH_S:
                    try:
                        self.flush()
                    except Exception:
                        self.log.exception("flush failed")
        finally:
            stream.close()
        self.log.info("%s stopped", self.name)
