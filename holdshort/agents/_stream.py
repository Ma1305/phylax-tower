"""Shared plumbing for agent processes: a stoppable change-stream loop and a CLI runner."""

from __future__ import annotations

import argparse
import logging
import signal
import sys
import threading
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from pymongo.database import Database

from holdshort.db import get_client
from holdshort.env import mongodb_db_name, redact

ChangeHandler = Callable[[dict[str, Any]], None]


def now() -> datetime:
    return datetime.now(UTC)


class StreamAgent:
    """Watches ``collection`` for inserts and calls ``handle(full_document)`` for each.

    Starts at "now" (agents react to live traffic, they do not replay history).
    ``run()`` blocks until ``stop()``; ``ready`` is set once the stream is open.
    """

    name = "agent"
    collection = ""

    def __init__(self, db: Database, max_await_ms: int = 300):
        self.db = db
        self.max_await_ms = max_await_ms
        self.log = logging.getLogger(f"holdshort.agents.{self.name}")
        self.ready = threading.Event()
        self._stop = threading.Event()
        self.processed = 0

    def pipeline(self) -> list[dict]:
        return [{"$match": {"operationType": "insert"}}]

    def handle(self, doc: dict[str, Any]) -> None:  # pragma: no cover - overridden
        raise NotImplementedError

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        with self.db[self.collection].watch(
            self.pipeline(), max_await_time_ms=self.max_await_ms
        ) as stream:
            self.ready.set()
            self.log.info("READY %s watching %s.%s", self.name, self.db.name, self.collection)
            while not self._stop.is_set():
                change = stream.try_next()
                if change is None:
                    continue
                try:
                    self.handle(change["fullDocument"])
                except Exception:
                    self.log.exception("failed to handle %s", change.get("documentKey"))
                self.processed += 1
        self.log.info("%s stopped", self.name)


def main(agent_cls: type[StreamAgent], argv: list[str] | None = None) -> int:
    """Run one agent as its own process (``python -m holdshort.agents.<name>``)."""
    parser = argparse.ArgumentParser(prog=f"python -m holdshort.agents.{agent_cls.name}_agent")
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
