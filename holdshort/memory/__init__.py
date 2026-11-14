"""Working memory (``context``): what agents search instead of the permanent log.

    python -m holdshort.memory [--db NAME]

``Memory`` watches transmissions / decisions / weather inserts and objects deletes, writes one
``context`` item per fact (see ``writer``), and sweeps expired items every ``SWEEP_S``.
Agents read it through ``holdshort.memory.query`` (search / brief / stats).
"""

from __future__ import annotations

import threading
from typing import Any

from pymongo.database import Database

from holdshort.agents._stream import StreamAgent
from holdshort.memory.writer import LIFETIMES_S, SUPERSEDED_GRACE_S, Writer

SWEEP_S = 30.0

__all__ = ["LIFETIMES_S", "SUPERSEDED_GRACE_S", "SWEEP_S", "Memory", "Writer"]


class Memory(StreamAgent):
    name = "memory"
    watch_collections = ["transmissions", "decisions", "weather", "objects"]
    operation_types = ("insert", "delete")

    def __init__(self, db: Database, *, lifetimes: dict[str, float] | None = None,
                 grace_s: float = SUPERSEDED_GRACE_S, sweep_s: float = SWEEP_S, **kw: Any):
        super().__init__(db, **kw)
        self.writer = Writer(db, {**LIFETIMES_S, **(lifetimes or {})}, grace_s)
        self.sweep_s = sweep_s
        self._sweeper: threading.Thread | None = None

    def pipeline(self) -> list[dict]:
        return [{"$match": {"$or": [
            {"operationType": "insert",
             "ns.coll": {"$in": ["transmissions", "decisions", "weather"]}},
            {"operationType": "delete", "ns.coll": "objects"},
        ]}}]

    def handle(self, doc: dict[str, Any], coll: str | None = None) -> None:
        if coll == "transmissions":
            self.writer.from_transmission(doc)
        elif coll == "decisions":
            self.writer.from_decision(doc)
        elif coll == "weather":
            self.writer.from_weather(doc)
        elif coll == "objects":
            self.writer.object_gone(doc.get("_id"))

    def _sweep_loop(self) -> None:
        while not self._stop.wait(self.sweep_s):
            try:
                n = self.writer.sweep()
                if n:
                    self.log.info("swept %d expired context items", n)
            except Exception:
                self.log.exception("sweep failed")

    def on_ready(self) -> None:
        self.writer.sweep()
        self._sweeper = threading.Thread(target=self._sweep_loop, name="memory-sweep",
                                         daemon=True)
        self._sweeper.start()
