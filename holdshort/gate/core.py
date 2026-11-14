"""Action gate implementation (see holdshort/gate/__init__.py for the overview).

Reason codes written to ``Decision.rule``:

- ``agent_disabled``      the agent's switch or the master "system" switch is off
- ``tier_never``          the agent's tier is "never"
- ``no_permission``       no permission document for the agent
- ``no_active_rule_set``  no rule set is active
- ``tier_autonomous``     executed without a controller (stop/close/reopen, autonomous tier)
- ``needs_approval``      waiting for a controller (any propose_instruction, or needs_approval tier)
- ``controller_approved`` executed after approve(); approved_by = controller
- ``controller_revoked``  revoke(); revokes = the revoked decision, approved_by = controller

Decision.t is scenario time: the initial decision uses the intent's t; approvals and
revocations use the referenced decision's t plus the wall-clock time elapsed since it
was inserted (so the audit timeline stays in scenario order).

Exactly-once: the change stream resume token is stored in gate_state after every event
(and after idle batches). On restart the gate resumes from it. Before inserting it
checks whether the intent already has an initial decision, so a crash between the
insert and the token write does not produce a duplicate.
"""

from __future__ import annotations

import logging
import re
import threading
from datetime import UTC, datetime
from typing import Any, get_args

from bson import ObjectId
from pymongo.database import Database
from pymongo.errors import PyMongoError

from holdshort import contracts as c

log = logging.getLogger("holdshort.gate")

INTENT_COLLECTION_RE = re.compile(r"^intents_([a-z]+)$")
GATE_STATE_ID = "gate"
AUTONOMOUS_ACTIONS = ("stop", "close_runway", "reopen_runway")
_ACTIONS = set(get_args(c.IntentAction))
_AGENTS = set(get_args(c.AgentName))


class GateError(Exception):
    """An approve/revoke/set_permission request that cannot be honoured."""


def _now() -> datetime:
    return datetime.now(UTC)


def _active_rule_set(db: Database) -> dict | None:
    return db.rules.find_one({"active": True}, sort=[("version", 1)])


def _insert_decision(db: Database, **fields: Any) -> dict:
    """Validate against the contract, insert, and return the stored doc (with _id)."""
    doc = c.to_mongo(c.Decision.model_validate(fields))
    doc["_id"] = db.decisions.insert_one(doc).inserted_id
    return doc


def _scenario_time(ref: dict) -> datetime:
    """ref.t + wall time since ref was inserted (keeps decisions in scenario order)."""
    t = ref["t"]
    if t.tzinfo is None:
        t = t.replace(tzinfo=UTC)
    oid = ref.get("_id")
    if isinstance(oid, ObjectId):
        elapsed = _now() - oid.generation_time
        if elapsed.total_seconds() > 0:
            t = t + elapsed
    return t


def _initial_decision(db: Database, intent_id: ObjectId) -> dict | None:
    return db.decisions.find_one({"intent_id": intent_id, "approved_by": None, "revokes": None})


def decide(db: Database, agent: str, intent: dict) -> dict | None:
    """Insert the gate's decision for one intent. ``agent`` comes from the collection name.

    Returns the decision, the existing one if the intent was already decided, or None
    when the intent cannot be represented as a decision (unknown agent/action).
    """
    intent_id = intent["_id"]
    existing = _initial_decision(db, intent_id)
    if existing is not None:
        log.info("intent %s already decided (%s); skipping", intent_id, existing["_id"])
        return existing
    action = intent.get("action")
    if agent not in _AGENTS or action not in _ACTIONS:
        log.warning(
            "ignoring intent %s: agent=%r action=%r not in contract", intent_id, agent, action
        )
        return None

    perm = db.permissions.find_one({"agent": agent})
    master = db.permissions.find_one({"agent": "system"})
    rule_set = _active_rule_set(db)
    version = rule_set["version"] if rule_set else "none"
    ref_perm = perm or master
    if ref_perm is None:
        log.error("intent %s: no permission documents at all; cannot decide", intent_id)
        return None

    if perm is None:
        outcome, rule = "rejected", "no_permission"
    elif master is None or not master.get("enabled") or not perm.get("enabled"):
        outcome, rule = "rejected", "agent_disabled"
    elif perm.get("tier") == "never":
        outcome, rule = "rejected", "tier_never"
    elif rule_set is None:
        outcome, rule = "rejected", "no_active_rule_set"
    elif action in AUTONOMOUS_ACTIONS and perm.get("tier") == "autonomous":
        outcome, rule = "executed", "tier_autonomous"
    else:
        outcome, rule = "pending_approval", "needs_approval"

    t = intent.get("t") or _now()
    if t.tzinfo is None:
        t = t.replace(tzinfo=UTC)
    decision = _insert_decision(
        db,
        t=t,
        intent_id=intent_id,
        agent=agent,
        intent_action=action,
        outcome=outcome,
        rule=rule,
        authorized_by={"permission_id": ref_perm["_id"], "rule_set_version": version},
        notified=list(intent.get("target") or []) if outcome == "executed" else [],
    )
    log.info("intent %s from %s (%s) -> %s [%s]", intent_id, agent, action, outcome, rule)
    return decision


def approve(db: Database, intent_id: ObjectId | str, controller: str) -> dict:
    """Controller approves a pending intent: inserts a NEW executed decision."""
    intent_id = ObjectId(intent_id) if isinstance(intent_id, str) else intent_id
    if not controller:
        raise GateError("controller is required")
    decisions = list(db.decisions.find({"intent_id": intent_id}).sort("_id", 1))
    pending = next((d for d in decisions if d["outcome"] == "pending_approval"), None)
    if pending is None:
        raise GateError(f"intent {intent_id} has no pending_approval decision")
    revoked_ids = {d.get("revokes") for d in decisions if d["outcome"] == "revoked"}
    if pending["_id"] in revoked_ids:
        raise GateError(f"pending decision {pending['_id']} was revoked")
    if any(d["outcome"] == "executed" for d in decisions):
        raise GateError(f"intent {intent_id} is already executed")
    agent = pending["agent"]
    perm = db.permissions.find_one({"agent": agent})
    master = db.permissions.find_one({"agent": "system"})
    if not (perm and perm.get("enabled") and master and master.get("enabled")):
        raise GateError(f"agent {agent!r} or the system switch is disabled")
    intent = db[f"intents_{agent}"].find_one({"_id": intent_id}) or {}
    rule_set = _active_rule_set(db)
    decision = _insert_decision(
        db,
        t=_scenario_time(pending),
        intent_id=intent_id,
        agent=agent,
        intent_action=pending["intent_action"],
        outcome="executed",
        rule="controller_approved",
        authorized_by={
            "permission_id": perm["_id"],
            "rule_set_version": rule_set["version"] if rule_set else "none",
        },
        approved_by=controller,
        notified=list(intent.get("target") or []),
    )
    log.info("intent %s approved by %s -> decision %s", intent_id, controller, decision["_id"])
    return decision


def revoke(db: Database, decision_id: ObjectId | str, controller: str) -> dict:
    """Controller revokes a decision: inserts a NEW revoked decision with revokes=<id>."""
    decision_id = ObjectId(decision_id) if isinstance(decision_id, str) else decision_id
    if not controller:
        raise GateError("controller is required")
    target = db.decisions.find_one({"_id": decision_id})
    if target is None:
        raise GateError(f"decision {decision_id} not found")
    if target["outcome"] == "revoked":
        raise GateError(f"decision {decision_id} is itself a revocation")
    if db.decisions.find_one({"revokes": decision_id}):
        raise GateError(f"decision {decision_id} is already revoked")
    perm = db.permissions.find_one({"agent": target["agent"]})
    rule_set = _active_rule_set(db)
    decision = _insert_decision(
        db,
        t=_scenario_time(target),
        intent_id=target["intent_id"],
        agent=target["agent"],
        intent_action=target["intent_action"],
        outcome="revoked",
        rule="controller_revoked",
        authorized_by={
            "permission_id": perm["_id"] if perm else target["authorized_by"]["permission_id"],
            "rule_set_version": rule_set["version"] if rule_set else "none",
        },
        approved_by=controller,
        revokes=decision_id,
        notified=list(target.get("notified") or []),
    )
    log.info("decision %s revoked by %s -> decision %s", decision_id, controller, decision["_id"])
    return decision


def set_permission(db: Database, agent: str, enabled: bool, by: str) -> dict:
    """Flip an agent's (or the master "system") switch. Returns the updated permission."""
    if agent not in get_args(c.PermissionAgent):
        raise GateError(f"unknown agent {agent!r}")
    if not by:
        raise GateError("by is required")
    doc = db.permissions.find_one_and_update(
        {"agent": agent},
        {"$set": {"enabled": bool(enabled), "by": by, "changed_at": _now()}},
        return_document=True,
    )
    if doc is None:
        raise GateError(f"no permission document for {agent!r} (run scripts/setup_atlas.py)")
    log.info("permission %s enabled=%s by %s", agent, bool(enabled), by)
    return doc


class Gate:
    """Watches intents_* with one change stream and decides each new intent.

    ``run()`` blocks until ``stop()`` is called (safe to run in a thread).
    """

    PIPELINE = [
        {"$match": {"operationType": "insert", "ns.coll": {"$regex": INTENT_COLLECTION_RE.pattern}}}
    ]

    def __init__(self, db: Database, max_await_ms: int = 500):
        self.db = db
        self.max_await_ms = max_await_ms
        self._stop = threading.Event()
        self.ready = threading.Event()
        """Set once the change stream is open (tests wait on it)."""
        self.processed = 0

    def stop(self) -> None:
        self._stop.set()

    def _load_token(self) -> dict | None:
        state = self.db.gate_state.find_one({"_id": GATE_STATE_ID})
        return state.get("resume_token") if state else None

    def _save_token(self, token: dict | None) -> None:
        if token is None:
            return
        self.db.gate_state.update_one(
            {"_id": GATE_STATE_ID},
            {"$set": {"resume_token": token, "updated_at": _now()}},
            upsert=True,
        )

    def handle(self, change: dict) -> dict | None:
        coll = change["ns"]["coll"]
        m = INTENT_COLLECTION_RE.match(coll)
        if not m:
            return None
        # identity = the collection name, never the document's "agent" field
        return decide(self.db, m.group(1), change["fullDocument"])

    def run(self) -> None:
        token = self._load_token()
        log.info("gate starting on %s (%s)", self.db.name, "resuming" if token else "fresh start")
        with self.db.watch(
            self.PIPELINE, resume_after=token, max_await_time_ms=self.max_await_ms
        ) as stream:
            last = token
            if stream.resume_token != last:
                self._save_token(stream.resume_token)
                last = stream.resume_token
            self.ready.set()
            while not self._stop.is_set():
                change = stream.try_next()
                if change is not None:
                    try:
                        self.handle(change)
                    except PyMongoError:
                        log.exception(
                            "failed to decide %s; will retry on restart", change.get("documentKey")
                        )
                        raise
                    self.processed += 1
                if stream.resume_token != last:
                    self._save_token(stream.resume_token)
                    last = stream.resume_token
        log.info("gate stopped")
