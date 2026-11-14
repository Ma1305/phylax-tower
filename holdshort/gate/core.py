"""Action gate v2 (see holdshort/gate/__init__.py for the overview).

Decision table (docs/v2/CONTRACTS.md), reason codes written to ``Decision.rule``:

- ``no_permission``       no permission document for the agent             -> rejected
- ``agent_disabled``      the agent's switch or the master "system" switch is off -> rejected
- ``tier_never``          the agent's tier is "never"                      -> rejected
- ``no_active_rule_set``  no rule set is active                            -> rejected
- ``advisory``            a ``warn``: executed, shown, never spoken
- ``mode_reactive``       a reactive agent asked for anything but ``warn`` -> rejected
- ``unknown_rule_set``    set_rule_set names a version that does not exist -> rejected
- ``tier_autonomous``     stop / close_lane / reopen_lane / set_rule_set run without a
                          controller (autonomous tier)
- ``needs_approval``      pending_approval: any propose_instruction, or a needs_approval tier
- ``controller_approved`` approve(): a NEW executed decision, approved_by = controller
- ``controller_rejected`` reject(): a NEW revoked decision, revokes = the pending one
- ``controller_revoked``  revoke(): a NEW revoked decision, revokes = the revoked one

Routing: ``lane_id`` from the intent, ``controller_id = owner_of(lane_id)`` (the supervisor
when nobody owns it), ``stream_id`` = the intent's, else the owner's first stream. Only that
controller or a supervisor may approve, reject or revoke; anyone else gets
``GateError("not_lane_owner")`` and a ``security_events`` document.

Side effects the gate owns when a decision executes: ``set_rule_set`` (activate the rule set
named in ``proposal_text``), ``close_lane`` / ``reopen_lane`` (lane_status.state closed /
clear). Each also inserts the matching ``state_events`` document.

Exactly-once: the change stream resume token is stored in ``gate_state`` after every event.
On restart the gate resumes from it, and before inserting it checks whether the intent already
has an initial decision, so a crash between the insert and the token write is harmless.
"""

from __future__ import annotations

import logging
import re
import threading
from datetime import UTC, datetime
from typing import Any, get_args

from bson import ObjectId
from bson.errors import InvalidId
from pymongo.database import Database
from pymongo.errors import PyMongoError

from holdshort import contracts as c
from holdshort import world
from holdshort.events import log_state_event

log = logging.getLogger("holdshort.gate")

INTENT_COLLECTION_RE = re.compile(r"^intents_([a-z]+)$")
GATE_STATE_ID = "gate"
AUTONOMOUS_ACTIONS = ("stop", "close_lane", "reopen_lane", "set_rule_set")
_ACTIONS = set(get_args(c.IntentAction))
_AGENTS = set(get_args(c.AgentName))
_MODES = set(get_args(c.AgentMode))
_TIERS = set(get_args(c.PermissionTier))
_SEVERITIES = set(get_args(c.Severity))

# GateError codes -> HTTP status on the control API
ERROR_STATUS = {
    "bad_input": 400,
    "not_lane_owner": 403,
    "not_found": 404,
    "already_decided": 409,
    "agent_disabled": 409,
}


class GateError(Exception):
    """A request the gate cannot honour. ``code`` is one of ERROR_STATUS; str(e) == code."""

    def __init__(self, code: str, detail: str | None = None):
        super().__init__(code)
        self.code = code
        self.detail = detail or code

    @property
    def status(self) -> int:
        return ERROR_STATUS.get(self.code, 400)


def _now() -> datetime:
    return datetime.now(UTC)


def _oid(value: ObjectId | str, what: str = "id") -> ObjectId:
    if isinstance(value, ObjectId):
        return value
    try:
        return ObjectId(str(value))
    except (InvalidId, TypeError) as e:
        raise GateError("bad_input", f"{what} {value!r} is not an ObjectId") from e


def _active_rule_set(db: Database) -> dict | None:
    return db.rules.find_one({"active": True}, sort=[("version", 1)])


def _insert_decision(db: Database, **fields: Any) -> dict:
    """Validate against the contract, insert, and return the stored doc (with _id)."""
    doc = c.to_mongo(c.Decision.model_validate(fields))
    doc["_id"] = db.decisions.insert_one(doc).inserted_id
    return doc


# ------------------------------------------------------------------ side effects


def activate_rule_set(db: Database, version: str, by: str | None = None) -> dict:
    """Make ``version`` the only active rule set and log a ``rule_set`` state event."""
    if db.rules.find_one({"version": version}) is None:
        raise GateError("bad_input", f"unknown rule set {version!r}")
    # activate first so there is never a moment with no active rule set
    db.rules.update_one({"version": version}, {"$set": {"active": True}})
    db.rules.update_many({"version": {"$ne": version}, "active": True}, {"$set": {"active": False}})
    doc = db.rules.find_one({"version": version})
    log_state_event(db, "rule_set", version, doc, by)
    log.info("rule set %s is now active", version)
    return doc


def _set_lane(db: Database, lane_id: str | None, state: str, reason: str | None,
              by: str | None) -> dict | None:
    if not lane_id:
        log.warning("lane change without a lane_id; ignored")
        return None
    doc = db.lane_status.find_one_and_update(
        {"_id": lane_id},
        {"$set": {"state": state, "reason": reason, "updated_at": _now()}},
        return_document=True,
    )
    if doc is None:
        log.warning("no lane_status for %s; ignored", lane_id)
        return None
    log_state_event(db, "lane_status", lane_id, doc, by, tower_id=doc.get("tower_id", "LGA"))
    log.info("lane %s -> %s (by %s)", lane_id, state, by)
    return doc


def close_lane(db: Database, lane_id: str | None, reason: str | None = None,
               by: str | None = None) -> dict | None:
    return _set_lane(db, lane_id, "closed", reason or "closed", by)


def reopen_lane(db: Database, lane_id: str | None, by: str | None = None) -> dict | None:
    return _set_lane(db, lane_id, "clear", None, by)


def _apply(db: Database, action: str, intent: dict, decision: dict, by: str | None) -> None:
    """Side effects of an executed decision that the gate itself owns."""
    if action == "set_rule_set":
        activate_rule_set(db, intent.get("proposal_text") or "", by)
    elif action == "close_lane":
        close_lane(db, decision.get("lane_id"), intent.get("detail") or intent.get("reason"), by)
    elif action == "reopen_lane":
        reopen_lane(db, decision.get("lane_id"), by)


# ------------------------------------------------------------------ deciding intents


def _initial_decision(db: Database, intent_id: ObjectId) -> dict | None:
    return db.decisions.find_one(
        {"intent_id": intent_id, "approved_by": None, "revokes": None}
    )


def _outcome(db: Database, action: str, intent: dict, perm: dict | None,
             master: dict | None, rule_set: dict | None) -> tuple[str, str]:
    if perm is None:
        return "rejected", "no_permission"
    if master is None or not master.get("enabled") or not perm.get("enabled"):
        return "rejected", "agent_disabled"
    if perm.get("tier") == "never":
        return "rejected", "tier_never"
    if rule_set is None:
        return "rejected", "no_active_rule_set"
    if action == "warn":
        return "executed", "advisory"
    if perm.get("mode") != "proactive":
        return "rejected", "mode_reactive"
    if action == "propose_instruction":
        return "pending_approval", "needs_approval"
    if action == "set_rule_set" and not db.rules.find_one(
        {"version": intent.get("proposal_text") or ""}
    ):
        return "rejected", "unknown_rule_set"
    if action in AUTONOMOUS_ACTIONS and perm.get("tier") == "autonomous":
        return "executed", "tier_autonomous"
    return "pending_approval", "needs_approval"


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

    perm = db.permissions.find_one({"_id": agent})
    master = db.permissions.find_one({"_id": "system"})
    rule_set = _active_rule_set(db)
    outcome, rule = _outcome(db, action, intent, perm, master, rule_set)

    tower_id = intent.get("tower_id") or c.TOWER_ID
    lane_id = intent.get("lane_id")
    controller_id = world.owner_of(db, lane_id, tower_id) or "unassigned"
    stream_id = intent.get("stream_id") or world.stream_of(db, controller_id)
    text = intent.get("detail") if action == "warn" else intent.get("proposal_text")
    severity = intent.get("severity") if intent.get("severity") in _SEVERITIES else "info"
    target = list(intent.get("target") or [])
    decision = _insert_decision(
        db,
        t=_now(),
        tower_id=tower_id,
        intent_id=intent_id,
        agent=agent,
        intent_action=action,
        severity=severity,
        outcome=outcome,
        rule=rule,
        lane_id=lane_id,
        controller_id=controller_id,
        stream_id=stream_id,
        text=text or intent.get("detail"),
        target=target,
        mode=(perm or {}).get("mode") if (perm or {}).get("mode") in _MODES else "reactive",
        authorized_by={
            "permission_id": agent if perm else "system",
            "rule_set_version": rule_set["version"] if rule_set else None,
        },
        notified=[controller_id] + (target if outcome == "executed" else []),
    )
    log.info("intent %s from %s (%s) -> %s [%s] -> %s",
             intent_id, agent, action, outcome, rule, controller_id)
    if outcome == "executed":
        _apply(db, action, intent, decision, f"gate:{agent}")
    return decision


# ------------------------------------------------------------------ controller actions


def _authorize(db: Database, decision: dict, controller_id: str, action: str) -> dict:
    """The controller must own the decision or be a supervisor; else a security event."""
    if not controller_id:
        raise GateError("bad_input", "controller_id is required")
    ctl = db.controllers.find_one({"_id": controller_id})
    if ctl is not None and (
        controller_id == decision.get("controller_id") or ctl.get("role") == "supervisor"
    ):
        return ctl
    error = (f"{controller_id} may not {action} decision {decision['_id']} "
             f"(routed to {decision.get('controller_id')})")
    db.security_events.insert_one(c.to_mongo(c.SecurityEvent(
        t=_now(), controller_id=controller_id, kind="not_lane_owner",
        attempted=f"{action} {decision['_id']}", error_code=403, error=error,
    )))
    log.warning("not_lane_owner: %s", error)
    raise GateError("not_lane_owner", error)


def _load(db: Database, decision_id: ObjectId | str) -> dict:
    decision = db.decisions.find_one({"_id": _oid(decision_id, "decision_id")})
    if decision is None:
        raise GateError("not_found", f"decision {decision_id} not found")
    return decision


def _open_pending(db: Database, decision_id: ObjectId | str) -> dict:
    pending = _load(db, decision_id)
    if pending["outcome"] != "pending_approval":
        raise GateError("already_decided", f"decision {pending['_id']} is {pending['outcome']}")
    later = db.decisions.find_one({
        "intent_id": pending["intent_id"], "_id": {"$ne": pending["_id"]},
        "$or": [{"outcome": "executed"}, {"revokes": pending["_id"]}],
    })
    if later is not None:
        raise GateError("already_decided", f"decision {pending['_id']} was already decided")
    return pending


def _follow_up(pending: dict, **fields: Any) -> dict:
    keep = ("tower_id", "intent_id", "agent", "intent_action", "severity", "lane_id",
            "controller_id", "stream_id", "text", "target", "mode", "authorized_by")
    out = {k: pending.get(k) for k in keep}
    out["t"] = _now()
    out.update(fields)
    return out


def approve(db: Database, decision_id: ObjectId | str, controller_id: str) -> dict:
    """Approve a pending decision: inserts a NEW executed decision (controller_approved)."""
    pending = _open_pending(db, decision_id)
    _authorize(db, pending, controller_id, "approve")
    agent = pending["agent"]
    perm = db.permissions.find_one({"_id": agent})
    master = db.permissions.find_one({"_id": "system"})
    if not (perm and perm.get("enabled") and master and master.get("enabled")):
        raise GateError("agent_disabled", f"agent {agent!r} or the system switch is off")
    intent = db[f"intents_{agent}"].find_one({"_id": pending["intent_id"]}) or {}
    rule_set = _active_rule_set(db)
    decision = _insert_decision(db, **_follow_up(
        pending,
        outcome="executed",
        rule="controller_approved",
        authorized_by={"permission_id": agent,
                       "rule_set_version": rule_set["version"] if rule_set else None},
        approved_by=controller_id,
        notified=list(pending.get("target") or []),
    ))
    _apply(db, pending["intent_action"], intent, decision, controller_id)
    log.info("decision %s approved by %s -> %s", pending["_id"], controller_id, decision["_id"])
    return decision


def reject(db: Database, decision_id: ObjectId | str, controller_id: str,
           reason: str | None = None) -> dict:
    """Reject a pending decision: inserts a NEW revoked decision (controller_rejected)."""
    pending = _open_pending(db, decision_id)
    _authorize(db, pending, controller_id, "reject")
    decision = _insert_decision(db, **_follow_up(
        pending,
        outcome="revoked",
        rule="controller_rejected",
        text=reason or pending.get("text"),
        approved_by=controller_id,
        revokes=pending["_id"],
        notified=[],
    ))
    log.info("decision %s rejected by %s -> %s", pending["_id"], controller_id, decision["_id"])
    return decision


def revoke(db: Database, decision_id: ObjectId | str, controller_id: str) -> dict:
    """Revoke a decision: inserts a NEW revoked decision with revokes = <id>."""
    target = _load(db, decision_id)
    if target["outcome"] == "revoked":
        raise GateError("already_decided", f"decision {target['_id']} is itself a revocation")
    _authorize(db, target, controller_id, "revoke")
    if db.decisions.find_one({"revokes": target["_id"]}):
        raise GateError("already_decided", f"decision {target['_id']} is already revoked")
    decision = _insert_decision(db, **_follow_up(
        target,
        outcome="revoked",
        rule="controller_revoked",
        approved_by=controller_id,
        revokes=target["_id"],
        notified=list(target.get("notified") or []),
    ))
    log.info("decision %s revoked by %s -> %s", target["_id"], controller_id, decision["_id"])
    return decision


def set_permission(db: Database, agent: str, by: str, enabled: bool | None = None,
                   mode: str | None = None, tier: str | None = None) -> dict:
    """Change an agent's (or the master "system") switch, mode or tier.

    Applies from the next intent. Logs a ``permission`` state event with the document after.
    """
    if agent not in get_args(c.PermissionAgent):
        raise GateError("not_found", f"unknown agent {agent!r}")
    if not by:
        raise GateError("bad_input", "by is required")
    changes: dict[str, Any] = {}
    if enabled is not None:
        changes["enabled"] = bool(enabled)
    if mode is not None:
        if mode not in _MODES:
            raise GateError("bad_input", f"mode must be one of {sorted(_MODES)}")
        changes["mode"] = mode
    if tier is not None:
        if tier not in _TIERS:
            raise GateError("bad_input", f"tier must be one of {sorted(_TIERS)}")
        changes["tier"] = tier
    if not changes:
        raise GateError("bad_input", "nothing to change (enabled, mode or tier)")
    changes.update(by=by, changed_at=_now())
    doc = db.permissions.find_one_and_update(
        {"_id": agent}, {"$set": changes}, return_document=True
    )
    if doc is None:
        raise GateError("not_found", f"no permission document for {agent!r} (run setup_atlas)")
    log_state_event(db, "permission", agent, doc, by)
    log.info("permission %s %s by %s", agent,
             {k: v for k, v in changes.items() if k not in ("by", "changed_at")}, by)
    return doc


# ------------------------------------------------------------------ the process


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
