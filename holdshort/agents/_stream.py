"""Shared plumbing for worker processes: a resumable, stoppable change-stream loop + CLI.

Subclass ``StreamAgent``:

    class Safety(StreamAgent):
        name = "safety_agent"                        # agent_state _id
        watch_collections = ["objects", "context"]   # or collection = "objects"
        operation_types = ("insert", "update", "replace")

        def handle(self, doc, coll=None): ...

The agent opens one database-level change stream filtered on ``ns.coll`` and
``operationType``. After each handled event its resume token is stored in
``agent_state`` (``_id`` = ``self.name``); the next run resumes from it, or starts at
"now" if the token is gone or too old.
"""

from __future__ import annotations

import argparse
import inspect
import logging
import signal
import sys
import threading
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from pymongo.database import Database
from pymongo.errors import OperationFailure, PyMongoError

from holdshort.db import get_client
from holdshort.env import mongodb_db_name, redact

ChangeHandler = Callable[[dict[str, Any]], None]


def now() -> datetime:
    return datetime.now(UTC)


class StreamAgent:
    """Watches ``watch_collections`` (default ``[collection]``) and calls ``handle`` per event.

    ``on_ready()`` runs once the stream is open and before READY is logged.
    ``run()`` blocks until ``stop()``; ``ready`` is set once the stream is open.
    """

    name = "agent"
    collection = ""
    watch_collections: list[str] | None = None
    operation_types: tuple[str, ...] = ("insert",)
    store_token: bool = True

    def __init__(self, db: Database, max_await_ms: int = 300):
        self.db = db
        self.max_await_ms = max_await_ms
        self.log = logging.getLogger(f"holdshort.{self.name}")
        self.ready = threading.Event()
        self._stop = threading.Event()
        self.processed = 0
        self.resumed = False
        try:
            self._handle_takes_coll = "coll" in inspect.signature(self.handle).parameters
        except (TypeError, ValueError):
            self._handle_takes_coll = False

    # ---------------------------------------------------------------- overridable

    @property
    def collections(self) -> list[str]:
        return list(self.watch_collections or ([self.collection] if self.collection else []))

    def pipeline(self) -> list[dict]:
        match: dict[str, Any] = {"operationType": {"$in": list(self.operation_types)}}
        if self.collections:
            match["ns.coll"] = {"$in": self.collections}
        return [{"$match": match}]

    def handle(self, doc: dict[str, Any], coll: str | None = None) -> None:  # pragma: no cover
        raise NotImplementedError

    def process(self, doc: dict[str, Any], coll: str | None = None) -> Any:
        if self._handle_takes_coll:
            return self.handle(doc, coll=coll)
        return self.handle(doc)

    def on_ready(self) -> None:
        """Called once the change stream is open."""

    # -------------------------------------------------------------- resume token

    def load_token(self) -> dict | None:
        st = self.db.agent_state.find_one({"_id": self.name})
        return (st or {}).get("resume_token")

    def save_token(self, token: dict) -> None:
        self.db.agent_state.update_one(
            {"_id": self.name},
            {"$set": {"resume_token": token, "updated_at": now()}},
            upsert=True,
        )

    # ---------------------------------------------------------------------- loop

    def _open(self):
        kwargs: dict[str, Any] = {"max_await_time_ms": self.max_await_ms}
        if set(self.operation_types) & {"update", "replace"}:
            kwargs["full_document"] = "updateLookup"
        token = self.load_token() if self.store_token else None
        if token:
            try:
                stream = self.db.watch(self.pipeline(), resume_after=token, **kwargs)
                self.resumed = True
                return stream
            except OperationFailure as e:
                self.log.warning("resume token unusable (%s); starting at now", e.code)
        self.resumed = False
        return self.db.watch(self.pipeline(), **kwargs)

    def _safe_process(self, change: dict[str, Any]) -> None:
        coll = change.get("ns", {}).get("coll")
        doc = change.get("fullDocument") or dict(change.get("documentKey") or {})
        try:
            self.process(doc, coll=coll)
        except Exception:
            self.log.exception("failed to handle %s from %s", doc.get("_id"), coll)
        self.processed += 1
        if self.store_token:
            try:
                self.save_token(change["_id"])
            except PyMongoError:
                self.log.exception("could not store resume token")

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        stream = self._open()
        try:
            first = None
            if self.resumed:
                # a lost token fails on the first getMore: fall back to "now"
                try:
                    first = stream.try_next()
                except OperationFailure as e:
                    self.log.warning("resume failed (%s); starting at now", e.code)
                    stream.close()
                    self.db.agent_state.update_one({"_id": self.name},
                                                   {"$unset": {"resume_token": ""}})
                    stream = self._open()
            self.on_ready()
            self.ready.set()
            self.log.info("READY %s watching %s.%s%s", self.name, self.db.name,
                          ",".join(self.collections) or "*",
                          " (resumed)" if self.resumed else "")
            if first is not None:
                self._safe_process(first)
            while not self._stop.is_set():
                change = stream.try_next()
                if change is None:
                    continue
                self._safe_process(change)
        finally:
            stream.close()
        self.log.info("%s stopped", self.name)


def main(agent_cls: type[StreamAgent], argv: list[str] | None = None) -> int:
    """Run one worker as its own process (``--db NAME``; exits cleanly on SIGTERM)."""
    parser = argparse.ArgumentParser(prog=f"python -m {agent_cls.__module__}")
    parser.add_argument("--db", default=None, help="database (default: MONGODB_DB)")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    client = get_client()
    try:
        agent = agent_cls(client[args.db or mongodb_db_name()])
        signal.signal(signal.SIGTERM, lambda *_: agent.stop())
        try:
            agent.run()
        except KeyboardInterrupt:
            pass
        return 0
    except Exception as e:  # never leak the connection string
        print(f"FAIL: {type(e).__name__}: {redact(e)}", file=sys.stderr)
        return 1
    finally:
        client.close()
