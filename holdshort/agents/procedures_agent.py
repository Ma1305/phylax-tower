"""Procedures agent: after an executed stop for a crossing conflict, propose how to resume.

    uv run python -m holdshort.agents.procedures_agent [--db NAME]   # run the agent
    uv run python -m holdshort.agents.procedures_agent rogue [--db NAME]

It watches decisions. When the gate executes a safety ``stop`` whose intent guards a
runway crossing against a landing (target = [crossing vehicle, landing aircraft]), it
inserts a ``propose_instruction`` into intents_procedures, e.g.
"TRUCK1 cross runway 04 at D after AC8646 passes". It never clears anything: the gate
holds every proposal for a controller's approval.

``rogue()`` plays a misbehaving procedures agent for the demo: it tries to insert a
"clear" intent and to write an executed decision directly. Atlas's validators reject
both; each blocked attempt is recorded in security_events (insert-only).
"""

from __future__ import annotations

import argparse
import logging
import sys
from typing import Any

from bson import ObjectId
from pymongo import DESCENDING
from pymongo.database import Database
from pymongo.errors import PyMongoError, WriteError

from holdshort.agents._stream import StreamAgent, now
from holdshort.agents._stream import main as run_agent
from holdshort.contracts import Intent, SecurityEvent, to_mongo
from holdshort.db import get_client
from holdshort.env import mongodb_db_name, redact
from holdshort.safety.engine import RULE_NO_CROSSING, RULE_NO_LANDING_OCCUPIED

log = logging.getLogger("holdshort.agents.procedures")


def proposal_text(crossing: str, runway: str, at: str | None, landing: str) -> str:
    at_part = f" at {at}" if at else ""
    return f"{crossing} cross runway {runway}{at_part} after {landing} passes"


def is_crossing_conflict(intent: dict[str, Any]) -> bool:
    reason = intent.get("reason")
    if reason == RULE_NO_CROSSING:
        return True
    return reason == RULE_NO_LANDING_OCCUPIED and "cleared to cross" in (
        intent.get("conflicts_with") or ""
    )


def build_proposal(db: Database, decision: dict[str, Any], intent: dict[str, Any]) -> Intent:
    crossing, landing = intent["target"][0], intent["target"][1]
    runway = intent.get("runway")
    cross_tx = db.transmissions.find_one(
        {"kind": "clearance", "instruction": "cross", "to": crossing, "runway": runway},
        sort=[("t", DESCENDING), ("_id", DESCENDING)],
    )
    at = cross_tx.get("at") if cross_tx else None
    return Intent(
        t=decision["t"],
        agent="procedures",
        action="propose_instruction",
        target=[crossing],
        runway=runway,
        reason=f"stop executed ({intent['reason']}); resume the crossing once {landing} "
        f"has passed",
        conflicts_with=landing,
        proposal_text=proposal_text(crossing, runway, at, landing),
    )


class ProceduresAgent(StreamAgent):
    name = "procedures"
    collection = "decisions"

    def __init__(self, db: Database, max_await_ms: int = 300):
        super().__init__(db, max_await_ms)
        self._seen: set[ObjectId] = set()

    def pipeline(self) -> list[dict]:
        return [{"$match": {
            "operationType": "insert",
            "fullDocument.agent": "safety",
            "fullDocument.intent_action": "stop",
            "fullDocument.outcome": "executed",
        }}]

    def handle(self, decision: dict[str, Any]) -> Intent | None:
        intent_id = decision["intent_id"]
        if intent_id in self._seen:
            return None
        self._seen.add(intent_id)
        intent = self.db.intents_safety.find_one({"_id": intent_id})
        if not intent or len(intent.get("target") or []) < 2 or not is_crossing_conflict(intent):
            return None
        proposal = build_proposal(self.db, decision, intent)
        iid = self.db.intents_procedures.insert_one(to_mongo(proposal)).inserted_id
        self.log.info("intent %s: propose %r", iid, proposal.proposal_text)
        return proposal


# ------------------------------------------------------------------------ rogue


def _record(db: Database, attempted: str, err: PyMongoError | None) -> dict[str, Any]:
    if err is None:  # should never happen: the validator let it through
        code, msg = None, "NOT BLOCKED: the write was accepted"
    else:
        code = getattr(err, "code", None)
        details = getattr(err, "details", None) or {}
        msg = redact(details.get("errmsg") or str(err))
    event = SecurityEvent(t=now(), agent="procedures", attempted=attempted, error_code=code,
                          error=msg)
    doc = to_mongo(event)
    doc["_id"] = db.security_events.insert_one(doc).inserted_id
    log.warning("security event: %s -> %s %s", attempted, code, msg)
    return doc


def rogue(db: Database) -> list[dict[str, Any]]:
    """Two forbidden writes by a misbehaving procedures agent. Returns the security events."""
    t = now()
    events = []

    attempted = 'insert intents_procedures {action: "clear", target: [TRUCK1], runway: 04}'
    try:
        db.intents_procedures.insert_one({
            "t": t, "agent": "procedures", "action": "clear", "target": ["TRUCK1"],
            "runway": "04", "reason": "rogue: clear TRUCK1 to cross", "conflicts_with": None,
            "proposal_text": "TRUCK1 cleared to cross runway 04",
        })
        events.append(_record(db, attempted, None))
    except WriteError as e:
        events.append(_record(db, attempted, e))

    attempted = ("insert decisions {intent_action: propose_instruction, outcome: executed, "
                 "approved_by: none}")
    try:
        perm = db.permissions.find_one({"agent": "procedures"}) or {}
        db.decisions.insert_one({
            "t": t, "intent_id": ObjectId(), "agent": "procedures",
            "intent_action": "propose_instruction", "outcome": "executed",
            "rule": "rogue_self_approved",
            "authorized_by": {"permission_id": perm.get("_id", ObjectId()),
                              "rule_set_version": "normal_v1"},
            "approved_by": None, "revokes": None, "notified": ["TRUCK1"],
        })
        events.append(_record(db, attempted, None))
    except WriteError as e:
        events.append(_record(db, attempted, e))
    return events


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] != "rogue":
        return run_agent(ProceduresAgent, argv)
    parser = argparse.ArgumentParser(prog="python -m holdshort.agents.procedures_agent rogue")
    parser.add_argument("--db", default=None)
    args = parser.parse_args(argv[1:])
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    client = get_client()
    try:
        for ev in rogue(client[args.db or mongodb_db_name()]):
            print(f"{ev['_id']} code={ev['error_code']} {ev['attempted']}")
        return 0
    finally:
        client.close()


if __name__ == "__main__":
    sys.exit(main())
