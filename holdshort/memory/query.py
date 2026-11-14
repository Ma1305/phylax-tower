"""Query API for agents over the working memory (``context``).

    search(db, "LGA", lane_ids=["RWY_04_22"], kinds=["clearance"])   # newest first
    search(db, "LGA", text="vehicle on the runway")                   # $vectorSearch, or regex
    brief(db, "LGA", "RWY_04_22")    # compact text block for an LLM prompt (< 1500 tokens)
    stats(db)                        # live / superseded counts vs the permanent log

Expired items are never returned, even before the TTL monitor or the sweep removes them.
Semantic search uses the Vector Search index ``context_vector`` (automated Voyage
embedding on ``summary``, created by scripts/setup_search.py); while it is missing or not
queryable, ``text`` is a case-insensitive regex on ``summary``.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any

from pymongo.database import Database
from pymongo.errors import OperationFailure, PyMongoError

from holdshort.world import world

COLLECTION = "context"
VECTOR_INDEX = "context_vector"
EMBED_MODEL = "voyage-4"
VECTOR_DEFINITION = {
    "fields": [
        {"type": "autoEmbed", "modality": "text", "path": "summary", "model": EMBED_MODEL},
        {"type": "filter", "path": "tower_id"},
        {"type": "filter", "path": "kind"},
        {"type": "filter", "path": "lane_ids"},
    ]
}

BRIEF_TOKEN_LIMIT = 1500
BRIEF_CHAR_LIMIT = 4000  # conservative: timestamps and ids cost more than 4 chars/token
BRIEF_ITEMS = 20
BRIEF_OBJECTS = 12

_PROJECT = {"summary": 1, "kind": 1, "callsigns": 1, "lane_ids": 1, "stream_id": 1,
            "instruction": 1, "readback": 1, "flag": 1, "source": 1, "t": 1,
            "superseded_by": 1, "expires_at": 1, "tower_id": 1}


def _now() -> datetime:
    return datetime.now(UTC)


def _match(tower_id: str, lane_ids, callsigns, kinds, include_superseded) -> dict[str, Any]:
    q: dict[str, Any] = {"tower_id": tower_id, "expires_at": {"$gt": _now()}}
    if not include_superseded:
        q["superseded_by"] = None
    if lane_ids:
        q["lane_ids"] = {"$in": list(lane_ids)}
    if callsigns:
        q["callsigns"] = {"$in": list(callsigns)}
    if kinds:
        q["kind"] = {"$in": list(kinds)}
    return q


def vector_ready(db: Database) -> bool:
    try:
        for ix in db[COLLECTION].list_search_indexes(VECTOR_INDEX):
            return bool(ix.get("queryable"))
    except PyMongoError:
        pass
    return False


def _vector(db: Database, tower_id: str, text: str, match: dict, kinds, lane_ids,
            limit: int) -> list[dict]:
    flt: dict[str, Any] = {"tower_id": tower_id}
    if kinds:
        flt["kind"] = {"$in": list(kinds)}
    if lane_ids:
        flt["lane_ids"] = {"$in": list(lane_ids)}
    rest = {k: v for k, v in match.items() if k not in ("tower_id", "kind", "lane_ids")}
    return list(db[COLLECTION].aggregate([
        {"$vectorSearch": {"index": VECTOR_INDEX, "path": "summary", "query": text,
                           "filter": flt, "numCandidates": max(100, limit * 20),
                           "limit": limit * 5}},
        {"$match": rest},
        {"$limit": limit},
        {"$project": {**_PROJECT, "score": {"$meta": "vectorSearchScore"}}},
    ]))


def search(db: Database, tower_id: str, *, lane_ids: list[str] | None = None,
           callsigns: list[str] | None = None, kinds: list[str] | None = None,
           text: str | None = None, include_superseded: bool = False,
           limit: int = 20, method: str | None = None) -> list[dict]:
    """Live context items, newest first (by relevance when ``text`` uses vector search).

    method: "vector", "regex", or None (vector when the index is queryable).
    """
    match = _match(tower_id, lane_ids, callsigns, kinds, include_superseded)
    if text:
        if method is None:
            method = "vector" if vector_ready(db) else "regex"
        if method == "vector":
            try:
                return _vector(db, tower_id, text, match, kinds, lane_ids, limit)
            except OperationFailure:
                pass
        match["summary"] = {"$regex": re.escape(text), "$options": "i"}
    return list(db[COLLECTION].find(match, _PROJECT).sort("t", -1).limit(limit))


def _fmt_obj(o: dict) -> str:
    alt = f" {int(o['alt_ft'])}ft" if o.get("alt_ft") is not None else ""
    spd = f" {int(o['speed_kt'])}kt" if o.get("speed_kt") is not None else ""
    clr = o.get("clearance") or {}
    c = f" cleared {clr.get('instruction')} {clr.get('lane_id') or ''}".rstrip() if clr else ""
    hs = " HOLD-SHORT" if o.get("hold_short") else ""
    name = o.get("callsign") or o.get("label") or o["_id"]
    return f"- {name} ({o.get('kind')}, {o.get('status')}) on {o.get('lane_id')}{alt}{spd}{c}{hs}"


def _fmt_status(s: dict | None, lane_id: str) -> str:
    if not s:
        return f"{lane_id}: unknown"
    res = ",".join(f"{r.get('callsign')}:{r.get('instruction')}"
                   for r in s.get("reservations") or [])
    occ = ",".join(s.get("occupants") or [])
    out = f"{lane_id}: {s.get('state')}"
    if occ:
        out += f" occupants={occ}"
    if res:
        out += f" reserved={res}"
    if s.get("reason"):
        out += f" ({s['reason']})"
    return out


def _clip(lines: list[str], limit: int) -> str:
    out, used = [], 0
    for ln in lines:
        ln = ln if len(ln) <= 220 else ln[:219] + "…"
        if used + len(ln) + 1 > limit - 20:
            out.append("… (truncated)")
            break
        out.append(ln)
        used += len(ln) + 1
    return "\n".join(out)


def brief(db: Database, tower_id: str, lane_id: str, *, char_limit: int = BRIEF_CHAR_LIMIT) -> str:
    """A compact text block for an LLM prompt about one lane, under 1500 tokens.

    Lane status for the lane and the lanes it crosses, the objects on them, the latest
    weather, and the newest live context items about those lanes and those objects.
    """
    w = world(db, tower_id)
    lane = next((x for x in w["lanes"] if x["_id"] == lane_id), None)
    crosses = list((lane or {}).get("crosses") or [])
    lanes = [lane_id, *crosses]
    statuses = {s["_id"]: s for s in db.lane_status.find({"_id": {"$in": lanes}})}
    objs = list(db.objects.find({"tower_id": tower_id, "lane_id": {"$in": lanes}})
                .sort([("lane_id", 1), ("updated_at", -1)]).limit(BRIEF_OBJECTS))
    calls = [o.get("callsign") or o["_id"] for o in objs]
    items = search(db, tower_id, lane_ids=lanes, limit=BRIEF_ITEMS)
    if calls:
        seen = {i["_id"] for i in items}
        items += [i for i in search(db, tower_id, callsigns=calls, limit=BRIEF_ITEMS // 2)
                  if i["_id"] not in seen]
        items.sort(key=lambda i: i["t"], reverse=True)
        items = items[:BRIEF_ITEMS]
    wx = search(db, tower_id, kinds=["weather"], limit=1)

    name = (lane or {}).get("name") or lane_id
    lines = [f"LANE {lane_id} ({name}); crosses: {', '.join(crosses) or 'none'}",
             "STATUS:", *[f"- {_fmt_status(statuses.get(x), x)}" for x in lanes]]
    lines.append(f"OBJECTS ({len(objs)}):")
    lines += [_fmt_obj(o) for o in objs] or ["- none"]
    if wx:
        lines.append(f"WEATHER: {wx[0]['summary']}")
    lines.append(f"MEMORY ({len(items)} newest live items):")
    lines += [f"- [{i['kind']}] {i['summary']}" for i in items] or ["- none"]
    return _clip(lines, char_limit)


def stats(db: Database) -> dict[str, Any]:
    """Live / superseded context counts next to the size of the permanent log."""
    now = _now()
    live_q = {"expires_at": {"$gt": now}}
    by_kind = {d["_id"]: d["n"] for d in db.context.aggregate([
        {"$match": {**live_q, "superseded_by": None}},
        {"$group": {"_id": "$kind", "n": {"$sum": 1}}}])}
    out = {
        "live": db.context.count_documents({**live_q, "superseded_by": None}),
        "superseded": db.context.count_documents({**live_q, "superseded_by": {"$ne": None}}),
        "expired_pending": db.context.count_documents({"expires_at": {"$lte": now}}),
        "by_kind": by_kind,
        "log": {c: db[c].estimated_document_count()
                for c in ("transmissions", "decisions", "weather")},
        "vector_index": vector_ready(db),
    }
    return out
