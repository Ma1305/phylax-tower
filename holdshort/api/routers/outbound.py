"""GET /ingest/outbound: what Hold Short has told traffic (docs/v2/SIMULATOR_CONTRACT.md)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from pymongo.database import Database

from holdshort.api.deps import get_db

router = APIRouter()

MAX_CALLS = 200


def _iso(t: datetime | None) -> str | None:
    if t is None:
        return None
    if t.tzinfo is None:
        t = t.replace(tzinfo=UTC)
    return t.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _parse_since(since: str | None) -> datetime:
    if not since:
        return datetime.now(UTC) - timedelta(minutes=5)
    try:
        t = datetime.fromisoformat(since.strip().replace("Z", "+00:00").replace(" ", "+"))
    except ValueError:
        raise HTTPException(status_code=400, detail="since: must be ISO 8601") from None
    return t if t.tzinfo else t.replace(tzinfo=UTC)


@router.get("/ingest/outbound")
def outbound(
    db: Annotated[Database, Depends(get_db)],
    tower_id: Annotated[str, Query(min_length=1)],
    since: Annotated[str | None, Query()] = None,
) -> dict:
    now = datetime.now(UTC)
    t_since = _parse_since(since)
    txs = list(db.transmissions.find(
        {"tower_id": tower_id, "synthetic": True, "direction": "out",
         "decision_id": {"$ne": None}, "t": {"$gt": t_since}},
    ).sort("t", 1).limit(MAX_CALLS))
    decisions = {d["_id"]: d for d in db.decisions.find(
        {"_id": {"$in": [t["decision_id"] for t in txs]}})}
    calls = []
    for tx in txs:
        d = decisions.get(tx["decision_id"], {})
        calls.append({
            "t": _iso(tx["t"]),
            "decision_id": str(tx["decision_id"]),
            "stream_id": tx.get("stream_id"),
            "action": d.get("intent_action"),
            "target": d.get("target") or ([tx["to"]] if tx.get("to") else []),
            "lane_id": d.get("lane_id", tx.get("lane_id")),
            "text": d.get("text") or tx.get("transcript"),
            "approved_by": d.get("approved_by"),
            "clip_id": str(tx["clip_id"]) if tx.get("clip_id") else None,
        })
    return {"now": _iso(now), "calls": calls}
