"""/viz/live: one shared change stream fanned out to every WebSocket client."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time

import store

log = logging.getLogger("viz.live")

WATCHED = [
    "objects",
    "lane_status",
    "transmissions",
    "audio_clips",
    "decisions",
    "permissions",
    "weather",
]
TYPES = {
    "lane_status": "lane_status",
    "transmissions": "transmission",
    "audio_clips": "clip",
    "decisions": "decision",
    "permissions": "permission",
    "weather": "weather",
}
OBJECT_MIN_INTERVAL_S = 0.5  # at most two position messages per second per object
QUEUE_MAX = 2000


class Client:
    def __init__(self, tower_id: str):
        self.tower_id = tower_id
        self.queue: asyncio.Queue = asyncio.Queue(maxsize=QUEUE_MAX)
        self.overflowed = False


class LiveHub:
    def __init__(self, db):
        self.db = db
        self.clients: set[Client] = set()
        self.ready = False
        self._tasks: list[asyncio.Task] = []
        self._last_sent: dict[str, float] = {}
        self._held: dict[str, dict] = {}  # newest unsent object doc per object id

    def start(self) -> None:
        self._tasks = [asyncio.create_task(self._watch()), asyncio.create_task(self._flush())]

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

    def register(self, tower_id: str) -> Client:
        client = Client(tower_id)
        self.clients.add(client)
        return client

    def unregister(self, client: Client) -> None:
        self.clients.discard(client)

    def publish(self, msg_type: str, doc: dict, tower_id: str | None) -> None:
        msg = store.jsonable({"type": msg_type, "doc": doc})
        for client in self.clients:
            if tower_id is not None and client.tower_id != tower_id:
                continue
            try:
                client.queue.put_nowait(msg)
            except asyncio.QueueFull:
                client.overflowed = True  # too slow: the socket handler closes it

    # ------------------------------------------------------------ objects (coalesced)

    def _object(self, doc: dict) -> None:
        oid = str(doc["_id"])
        now = time.monotonic()
        if now - self._last_sent.get(oid, 0.0) >= OBJECT_MIN_INTERVAL_S and oid not in self._held:
            self._last_sent[oid] = now
            self.publish("object", doc, doc.get("tower_id"))
        else:
            self._held[oid] = doc

    async def _flush(self) -> None:
        while True:
            await asyncio.sleep(0.1)
            now = time.monotonic()
            for oid in [
                o for o in self._held if now - self._last_sent.get(o, 0.0) >= OBJECT_MIN_INTERVAL_S
            ]:
                doc = self._held.pop(oid)
                self._last_sent[oid] = now
                self.publish("object", doc, doc.get("tower_id"))
            if len(self._last_sent) > 5000:
                self._last_sent = {o: t for o, t in self._last_sent.items() if now - t < 60}

    # ------------------------------------------------------------ change stream

    async def _handle(self, change: dict) -> None:
        coll = change["ns"]["coll"]
        op = change["operationType"]
        doc = change.get("fullDocument")
        if coll == "objects":
            if op == "delete":
                oid = str(change["documentKey"]["_id"])
                self._held.pop(oid, None)
                self._last_sent.pop(oid, None)
                self.publish("object_removed", {"_id": oid}, None)
            elif doc:
                self._object(doc)
            return
        if not doc:
            return
        if coll == "audio_clips":
            doc = {**store.clip_view(doc), "tower_id": doc.get("tower_id")}
        elif coll == "decisions":
            await store.attach_intents(self.db, [doc])
        # permissions are tower-wide switches: every client gets them
        self.publish(TYPES[coll], doc, None if coll == "permissions" else doc.get("tower_id"))

    async def _watch(self) -> None:
        pipeline = [
            {
                "$match": {
                    "ns.coll": {"$in": WATCHED},
                    "operationType": {"$in": ["insert", "update", "replace", "delete"]},
                }
            }
        ]
        resume = None
        while True:
            try:
                kwargs = {"resume_after": resume} if resume else {}
                async with await self.db.watch(
                    pipeline, full_document="updateLookup", **kwargs
                ) as stream:
                    self.ready = True
                    async for change in stream:
                        resume = change["_id"]
                        try:
                            await self._handle(change)
                        except Exception:
                            log.exception("could not fan out a change")
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # network blip, lost token: reopen from now
                self.ready = False
                log.warning("change stream closed (%s); reopening", type(exc).__name__)
                resume = None
                await asyncio.sleep(2)
