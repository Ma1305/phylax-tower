"""LangGraph wrapper for agent loops, checkpointed to Atlas with MongoDBSaver.

Each agent step (one inserted document: a transmission for safety, a decision for
procedures) is one invocation of a small compiled StateGraph. Its thread_id is the
**scenario run id** (``scenario_runs._id``, the runner's marker), so every node's
output is checkpointed to ``checkpoints_<agent>`` / ``checkpoint_writes_<agent>`` in
the same database while the run plays.

A killed agent therefore resumes from Atlas instead of replaying history:

1. on start it finds the latest scenario run and loads that thread's checkpoint;
2. if the process died between two nodes, ``invoke(None)`` finishes that step from
   the checkpointed values (nodes already done are not run again);
3. it catches up only on documents inserted after the checkpoint's ``last_id``
   (missed while it was down) and then follows its change stream as usual,
   skipping anything at or before ``last_id``.

Nodes are plain deterministic Python (the safety decision is the rule engine; no LLM).
LangSmith tracing is switched on only if LANGSMITH_API_KEY is set (``configure_tracing``).
"""

from __future__ import annotations

import os
from collections.abc import Callable
from typing import Any

from bson import ObjectId
from langgraph.checkpoint.mongodb import MongoDBSaver
from langgraph.graph import END, START, StateGraph
from pymongo import ASCENDING, DESCENDING
from pymongo.database import Database

from holdshort.agents._stream import StreamAgent

Node = Callable[[dict[str, Any]], dict[str, Any]]

LANGSMITH_PROJECT = "hold-short"


def configure_tracing() -> bool:
    """LangSmith tracing on iff LANGSMITH_API_KEY is set (env is loaded by holdshort.env)."""
    if os.environ.get("LANGSMITH_API_KEY", "").strip():
        os.environ.setdefault("LANGSMITH_TRACING", "true")
        os.environ.setdefault("LANGSMITH_PROJECT", LANGSMITH_PROJECT)
        return os.environ["LANGSMITH_TRACING"].lower() == "true"
    os.environ["LANGSMITH_TRACING"] = "false"
    os.environ["LANGCHAIN_TRACING_V2"] = "false"
    return False


def current_run(db: Database, before: ObjectId | None = None) -> ObjectId | None:
    """The scenario run a document belongs to: the latest run started at or before it."""
    query = {"_id": {"$lte": before}} if before is not None else {}
    doc = db.scenario_runs.find_one(query, sort=[("_id", DESCENDING)], projection={"_id": 1})
    return doc["_id"] if doc else None


class GraphAgent(StreamAgent):
    """A StreamAgent whose per-document work is a checkpointed LangGraph graph.

    Subclasses define ``nodes()`` (ordered (name, fn) pairs, run in a line),
    ``graph_input(doc)`` (the JSON-safe input for one document) and
    ``catch_up_filter()`` (the find() equivalent of ``pipeline()``).
    """

    state_schema: type = dict

    def __init__(self, db: Database, max_await_ms: int = 300, checkpoints: bool = True):
        super().__init__(db, max_await_ms)
        self.tracing = configure_tracing()
        self.saver: MongoDBSaver | None = None
        self._last: dict[ObjectId, str | None] = {}
        """last_id of the thread this process is on (only one process runs an agent)."""
        self.graph = None
        if checkpoints:
            self.saver = MongoDBSaver(
                db.client,
                db_name=db.name,
                checkpoint_collection_name=f"checkpoints_{self.name}",
                writes_collection_name=f"checkpoint_writes_{self.name}",
            )
            self.graph = self.build_graph().compile(checkpointer=self.saver)

    # -- subclass API

    def nodes(self) -> list[tuple[str, Node]]:  # pragma: no cover - overridden
        raise NotImplementedError

    def graph_input(self, doc: dict[str, Any]) -> dict[str, Any]:  # pragma: no cover
        raise NotImplementedError

    def catch_up_filter(self) -> dict[str, Any]:
        return {}

    # -- graph

    def build_graph(self) -> StateGraph:
        g = StateGraph(self.state_schema)
        names = []
        for name, fn in self.nodes():
            g.add_node(name, fn)
            names.append(name)
        for a, b in zip([START, *names], [*names, END], strict=True):
            g.add_edge(a, b)
        return g

    @staticmethod
    def config(run_id: ObjectId) -> dict[str, Any]:
        return {"configurable": {"thread_id": str(run_id)}}

    def snapshot(self, run_id: ObjectId):
        return self.graph.get_state(self.config(run_id))

    def checkpoint_step(self, run_id: ObjectId) -> int | None:
        snap = self.snapshot(run_id)
        return (snap.metadata or {}).get("step") if snap and snap.values else None

    def _invoke(self, run_id: ObjectId, value: dict[str, Any] | None) -> dict[str, Any]:
        return self.graph.invoke(value, self.config(run_id), durability="sync")

    # -- loop

    def process(self, doc: dict[str, Any]) -> Any:
        if self.graph is None:
            return self.handle(doc)
        run_id = current_run(self.db, doc["_id"])
        if run_id is None:  # not part of a scenario run: nothing to checkpoint against
            return self.handle(doc)
        if run_id not in self._last:  # first document of this thread in this process
            snap = self.snapshot(run_id)
            if snap.next:  # died mid-step last time: finish it from the checkpoint first
                self._finish_interrupted(run_id, snap)
                snap = self.snapshot(run_id)
            self._last = {run_id: (snap.values or {}).get("last_id")}
        last = self._last[run_id]
        if last and doc["_id"] <= ObjectId(last):
            self.log.info("skip %s (already in checkpoint, last %s)", doc["_id"], last)
            return None
        out = self._invoke(run_id, self.graph_input(doc))
        self._last[run_id] = out.get("last_id")
        self.log.info("CHECKPOINT thread=%s step=%s doc=%s processed=%s", run_id,
                      self.checkpoint_step(run_id), doc["_id"], out.get("processed"))
        return out

    def _finish_interrupted(self, run_id: ObjectId, snap) -> None:
        self.log.info("RESUME thread=%s mid-step at checkpoint step=%s: running %s",
                      run_id, (snap.metadata or {}).get("step"), list(snap.next))
        self._invoke(run_id, None)

    def on_ready(self) -> None:
        """Resume the latest run's thread from Atlas and catch up on missed documents."""
        if self.graph is None:
            return
        run_id = current_run(self.db)
        if run_id is None:
            self.log.info("no scenario run yet; starting fresh")
            return
        snap = self.snapshot(run_id)
        if not snap.values:
            self.log.info("no checkpoint for thread=%s; starting fresh (no replay)", run_id)
            return
        if snap.next:
            self._finish_interrupted(run_id, snap)
            snap = self.snapshot(run_id)
        v = snap.values
        self._last = {run_id: v.get("last_id")}
        missed = self._missed(run_id, v.get("last_id"))
        self.log.info(
            "RESUME thread=%s from checkpoint step=%s (processed=%s, last=%s); "
            "catching up %d missed, not replaying history",
            run_id, (snap.metadata or {}).get("step"), v.get("processed"), v.get("last_id"),
            len(missed),
        )
        for doc in missed:
            self._safe_process(doc)

    def _missed(self, run_id: ObjectId, last_id: str | None) -> list[dict[str, Any]]:
        floor: dict[str, Any] = {"$gt": ObjectId(last_id)} if last_id else {"$gte": run_id}
        query = {**self.catch_up_filter(), "_id": floor}
        return list(self.db[self.collection].find(query).sort("_id", ASCENDING))
