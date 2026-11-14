"""Controller API: ``/control/*`` (docs/v2/CONTRACTS.md, "Control API").

Controller identity is the ``controller_id`` in the request (no login in this build). Every
action goes through holdshort.gate, so it is checked (lane owner or supervisor) and recorded
as a new decision / state event. Errors: 400 bad input, 403 not_lane_owner, 404 unknown id,
409 already decided.
"""

from __future__ import annotations

import importlib
from datetime import UTC, datetime
from typing import Annotated, Any

from bson import ObjectId
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from pymongo.database import Database
from starlette.concurrency import run_in_threadpool

from holdshort import gate
from holdshort.api.deps import get_db

Db = Annotated[Database, Depends(get_db)]

router = APIRouter(prefix="/control", tags=["control"])


def jsonable(v: Any) -> Any:
    """BSON -> JSON: ObjectId as str, datetimes as ISO 8601 UTC with Z."""
    if isinstance(v, ObjectId):
        return str(v)
    if isinstance(v, datetime):
        if v.tzinfo is None:
            v = v.replace(tzinfo=UTC)
        return v.astimezone(UTC).isoformat().replace("+00:00", "Z")
    if isinstance(v, dict):
        return {k: jsonable(x) for k, x in v.items()}
    if isinstance(v, list | tuple):
        return [jsonable(x) for x in v]
    return v


def _http(e: gate.GateError) -> HTTPException:
    return HTTPException(status_code=e.status, detail={"error": e.code, "detail": e.detail})


class DecisionBody(BaseModel):
    decision_id: str
    controller_id: str


class RejectBody(DecisionBody):
    reason: str | None = None


class PermissionBody(BaseModel):
    by: str
    enabled: bool | None = None
    mode: str | None = None  # validated by the gate (400 on a bad value)
    tier: str | None = None


@router.get("/controllers")
def controllers(db: Db, tower_id: str = "LGA") -> dict:
    docs = list(db.controllers.find({"tower_id": tower_id}).sort("_id", 1))
    return {"controllers": jsonable(docs)}


def open_pending(db: Database, controller_id: str | None = None) -> list[dict]:
    """Pending decisions with no later decision for the same intent, newest first."""
    q: dict[str, Any] = {"outcome": "pending_approval"}
    if controller_id:
        q["controller_id"] = controller_id
    pending = list(db.decisions.find(q).sort("t", -1).limit(500))
    if not pending:
        return []
    ids = [p["_id"] for p in pending]
    closed = set()
    for d in db.decisions.find(
        {"intent_id": {"$in": [p["intent_id"] for p in pending]},
         "$or": [{"outcome": "executed"}, {"revokes": {"$in": ids}}]},
        {"intent_id": 1, "revokes": 1, "outcome": 1},
    ):
        closed.add(d["revokes"] if d.get("revokes") else d["intent_id"])
    out = [p for p in pending if p["_id"] not in closed and p["intent_id"] not in closed]
    return _merge_intents(db, out)


@router.get("/pending")
def pending(controller_id: Annotated[str, Query()], db: Db) -> dict:
    ctl = db.controllers.find_one({"_id": controller_id})
    if ctl is None:
        raise HTTPException(404, {"error": "not_found", "detail": f"unknown {controller_id}"})
    scope = None if ctl.get("role") == "supervisor" else controller_id
    return {"pending": jsonable(open_pending(db, scope))}


def _merge_intents(db: Database, decisions: list[dict]) -> list[dict]:
    by_agent: dict[str, list[ObjectId]] = {}
    for d in decisions:
        if d.get("agent") and d.get("intent_id"):
            by_agent.setdefault(d["agent"], []).append(d["intent_id"])
    intents: dict[ObjectId, dict] = {}
    for agent, iids in by_agent.items():
        for i in db[f"intents_{agent}"].find({"_id": {"$in": iids}}):
            intents[i["_id"]] = i
    for d in decisions:
        d["intent"] = intents.get(d.get("intent_id"))
    return decisions


@router.get("/decisions")
def decisions(db: Db, tower_id: str = "LGA", controller_id: str | None = None,
              since: datetime | None = None,
              limit: Annotated[int, Query(ge=1, le=1000)] = 100) -> dict:
    """The decision log, newest first, each decision with its intent merged in as ``intent``."""
    q: dict[str, Any] = {"tower_id": tower_id}
    if controller_id:
        q["controller_id"] = controller_id
    if since is not None:
        q["t"] = {"$gte": since if since.tzinfo else since.replace(tzinfo=UTC)}
    docs = list(db.decisions.find(q).sort([("t", -1), ("_id", -1)]).limit(limit))
    return {"decisions": jsonable(_merge_intents(db, docs))}


@router.post("/approve")
def approve(body: DecisionBody, db: Db) -> dict:
    try:
        return jsonable(gate.approve(db, body.decision_id, body.controller_id))
    except gate.GateError as e:
        raise _http(e) from e


@router.post("/reject")
def reject(body: RejectBody, db: Db) -> dict:
    try:
        return jsonable(gate.reject(db, body.decision_id, body.controller_id, body.reason))
    except gate.GateError as e:
        raise _http(e) from e


@router.post("/revoke")
def revoke(body: DecisionBody, db: Db) -> dict:
    try:
        return jsonable(gate.revoke(db, body.decision_id, body.controller_id))
    except gate.GateError as e:
        raise _http(e) from e


@router.post("/permissions/{agent}")
def permissions(agent: str, body: PermissionBody, db: Db) -> dict:
    # `by` must be a real, active controller: an unknown id used to be accepted and written
    # into the audit trail as the author of the change
    ctl = db.controllers.find_one({"_id": body.by})
    if ctl is None or ctl.get("active") is False:
        raise HTTPException(
            404, {"error": "not_found", "detail": f"unknown controller {body.by!r}"})
    try:
        return jsonable(gate.set_permission(
            db, agent, body.by, enabled=body.enabled, mode=body.mode, tier=body.tier
        ))
    except gate.GateError as e:
        raise _http(e) from e


@router.get("/audit")
async def audit(q: Annotated[str, Query(min_length=1)], db: Db) -> Any:
    try:
        fn = importlib.import_module("holdshort.audit").answer
    except Exception as e:  # any import failure: the audit agent is not available
        raise HTTPException(501, {"error": "not_implemented", "detail": str(e)}) from e
    return jsonable(await run_in_threadpool(fn, db, q))
