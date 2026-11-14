"""Safety agent v2: lane_status / context changes -> deterministic rules -> intents_safety.

    uv run python -m holdshort.agents.safety_agent [--db NAME]

Triggers: change streams on ``lane_status`` (kept by the state service) and ``context``
(readback mismatches). Every read goes through ``scoped_db(db, "safety")``, so the agent can
only see what its permission document lists (context, objects, lane_status, lanes,
controllers, streams, rules, permissions, weather) and can only write ``intents_safety``.

Per new conflict: one ``warn``; in proactive mode and severity critical also one ``stop`` for
the party that can stop safely. Mode is read from the permission document on every event.
Conflicts are de-duplicated by key (rule, lane, sorted targets) while they last; the active
keys live in ``agent_state`` (``_id`` = ``safety_agent``, field ``active_conflicts``) next to
the resume token, so a restart does not repeat them. No LLM.
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from typing import Any

from pymongo.database import Database

from holdshort.access import AccessDenied, scoped_db
from holdshort.agents._stream import StreamAgent, main
from holdshort.contracts import TOWER_ID, Intent, to_mongo
from holdshort.safety.engine import (
    Conflict,
    LaneView,
    intents_for,
    lane_conflicts,
    lane_scope,
    new_conflicts,
    readback_conflicts,
)

AGENT = "safety"


class SafetyAgent(StreamAgent):
    name = "safety_agent"
    watch_collections = ["lane_status", "context"]
    operation_types = ("insert", "update", "replace")

    def __init__(self, db: Database, **kwargs: Any):
        super().__init__(db, **kwargs)
        self.scoped = scoped_db(db, AGENT)
        self.emitted: list[Intent] = []

    # ------------------------------------------------------------------ reads (scoped)

    def rule_ids(self) -> set[str]:
        doc = self.scoped.rules.find_one({"active": True}, sort=[("version", 1)])
        return {r["id"] for r in (doc or {}).get("rules") or []}

    def mode(self) -> str:
        p = self.scoped.permissions.find_one({"_id": AGENT}) or {}
        return p.get("mode") or "reactive"

    def lane_view(self, lane_id: str) -> LaneView:
        doc = self.scoped.lanes.find_one({"_id": lane_id}) or {}
        return LaneView(lane_id, doc.get("kind") or "taxiway", doc.get("designators") or [])

    def objects_for(self, ls: dict) -> dict[str, dict]:
        ids = list(ls.get("occupants") or [])
        ids += [r["object_id"] for r in ls.get("reservations") or [] if r.get("object_id")]
        cs = [r["callsign"] for r in ls.get("reservations") or [] if r.get("callsign")]
        q = {"$or": [{"_id": {"$in": ids}}, {"callsign": {"$in": cs}}]}
        return {o["_id"]: o for o in self.scoped.objects.find(q)}

    def stream_for(self, c: Conflict, controller_id: str | None) -> str | None:
        sp = c.stop_party
        if sp and sp.callsign:
            item = self.scoped.context.find_one(
                {"callsigns": sp.callsign, "stream_id": {"$ne": None}}, sort=[("t", -1)])
            if item:
                return item["stream_id"]
        if controller_id:
            ctl = self.scoped.controllers.find_one({"_id": controller_id}) or {}
            if ctl.get("streams"):
                return ctl["streams"][0]
        return None

    # ---------------------------------------------------- dedupe keys (agent_state)

    def load_keys(self) -> set[str]:
        st = self.db.agent_state.find_one({"_id": self.name}) or {}
        return set(st.get("active_conflicts") or [])

    def save_keys(self, keys: set[str]) -> None:
        self.db.agent_state.update_one(
            {"_id": self.name},
            {"$set": {"active_conflicts": sorted(keys), "updated_at": datetime.now(UTC)}},
            upsert=True)

    # ----------------------------------------------------------------------- handle

    def handle(self, doc: dict[str, Any], coll: str | None = None) -> list[Intent]:
        if coll == "context":
            if doc.get("readback") != "mismatch":
                return []
            return self.evaluate(readback_conflicts(doc, self.rule_ids()), [],
                                 doc.get("tower_id") or TOWER_ID, None)
        return self.evaluate_lane(doc)

    def evaluate_lane(self, ls: dict) -> list[Intent]:
        if not ls.get("_id"):
            return []
        lane = self.lane_view(ls["_id"])
        conflicts = lane_conflicts(ls, self.objects_for(ls), lane, self.rule_ids())
        return self.evaluate(conflicts, lane_scope(lane.id), ls.get("tower_id") or TOWER_ID,
                             ls.get("controller_id"))

    def evaluate(self, conflicts: list[Conflict], scope: list[str], tower_id: str,
                 controller_id: str | None) -> list[Intent]:
        active = self.load_keys()
        fresh, keys = new_conflicts(conflicts, active, scope)
        out: list[Intent] = []
        if fresh:
            mode, t = self.mode(), datetime.now(UTC)
            for c in fresh:
                ctl = controller_id
                if ctl is None and c.lane_id:
                    ls = self.scoped.lane_status.find_one({"_id": c.lane_id}) or {}
                    ctl = ls.get("controller_id")
                for intent in intents_for(c, mode, tower_id=tower_id, t=t,
                                          stream_id=self.stream_for(c, ctl)):
                    self.scoped.intents_safety.insert_one(to_mongo(intent))
                    self.log.info("%s %s %s %s: %s", intent.action, intent.severity,
                                  intent.reason, intent.target, intent.detail)
                    out.append(intent)
        if keys != active:
            self.save_keys(keys)
        self.emitted.extend(out)
        return out

    def on_ready(self) -> None:
        """Catch up on conflicts that already exist (keys in agent_state stop repeats)."""
        try:
            lanes = list(self.scoped.lane_status.find({}))
        except AccessDenied as e:
            self.log.warning("startup catch-up skipped: %s", e)
            return
        for ls in lanes:
            try:
                self.evaluate_lane(ls)
            except Exception:
                self.log.exception("startup evaluation failed for %s", ls.get("_id"))


if __name__ == "__main__":
    sys.exit(main(SafetyAgent))
