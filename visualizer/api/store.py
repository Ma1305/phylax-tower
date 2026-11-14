"""Read-only queries behind the /viz/* routes.

Time travel is rebuilt from stored records only: the `positions` time series, `state_events`
and the insert-only logs (`transmissions`, `decisions`, `weather`, `sim_runs`). Nothing in this
module writes to the database.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

from bson import ObjectId

OBJECT_WINDOW_S = 30  # an object is "there" at t if it reported in (t-30s, t]
OBJECT_TTL_S = 120
MAX_TRACK_WINDOW_S = 6 * 3600
MAX_TRACK_POINTS = 3600  # per object
MAX_MARKS = 2000  # per source
MAX_CLIPS = 500
PENDING_LOOKBACK = 200
ALERT_SEVERITIES = {"warning", "critical"}
INTENT_FIELDS = {
    "action": 1,
    "severity": 1,
    "target": 1,
    "object_ids": 1,
    "lane_id": 1,
    "runway": 1,
    "stream_id": 1,
    "reason": 1,
    "conflicts_with": 1,
    "proposal_text": 1,
    "detail": 1,
    "agent": 1,
    "t": 1,
}


def now_utc() -> datetime:
    return datetime.now(UTC)


def parse_t(value: str) -> datetime:
    """ISO 8601 (with Z or an offset; naive means UTC) -> aware UTC datetime."""
    text = value.strip().replace(" ", "+")  # a "+00:00" offset that lost its plus in a URL
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    dt = datetime.fromisoformat(text)
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


def jsonable(value: Any) -> Any:
    """BSON -> JSON: ObjectId as str, dates as ISO 8601 with Z."""
    if isinstance(value, dict):
        return {str(k): jsonable(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [jsonable(v) for v in value]
    if isinstance(value, ObjectId):
        return str(value)
    if isinstance(value, datetime):
        dt = value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
        return dt.isoformat(timespec="milliseconds").replace("+00:00", "Z")
    if isinstance(value, bytes):
        return None
    return value


def oid(value: str) -> ObjectId | None:
    return ObjectId(value) if ObjectId.is_valid(value) else None


# ---------------------------------------------------------------- world


async def world(db, tower_id: str, controller_id: str | None = None) -> dict | None:
    q = {"tower_id": tower_id}
    tower, lanes, holds, controllers, streams = await asyncio.gather(
        db.towers.find_one({"_id": tower_id}),
        db.lanes.find(q).to_list(500),
        db.hold_points.find(q).to_list(500),
        db.controllers.find(q).to_list(100),
        db.streams.find(q).to_list(100),
    )
    if tower is None:
        return None
    if controller_id:
        me = next((c for c in controllers if c["_id"] == controller_id), None)
        my_lanes = set(me.get("lanes", [])) if me else set()
        for lane in lanes:
            lane["mine"] = lane["_id"] in my_lanes
        for s in streams:
            s["mine"] = s.get("controller_id") == controller_id
    return {
        "tower": tower,
        "lanes": lanes,
        "hold_points": holds,
        "controllers": controllers,
        "streams": streams,
    }


async def bounds(db, tower_id: str) -> dict:
    """Earliest and latest t held in the records time travel is built from."""

    async def edge(coll: str, field: str, direction: int, q: dict):
        doc = await db[coll].find_one(q, {field: 1, "_id": 0}, sort=[(field, direction)])
        return doc.get(field) if doc else None

    sources = [
        ("positions", "t", {"meta.tower_id": tower_id}),
        ("transmissions", "t", {"tower_id": tower_id}),
        ("decisions", "t", {"tower_id": tower_id}),
        ("state_events", "t", {"tower_id": tower_id}),
    ]
    found = await asyncio.gather(*[edge(c, f, d, q) for c, f, q in sources for d in (1, -1)])
    lows = [v for v in found[0::2] if v]
    highs = [v for v in found[1::2] if v]
    return {"from": min(lows) if lows else None, "to": max(highs) if highs else None}


# ---------------------------------------------------------------- snapshot


async def attach_intents(db, decisions: list[dict]) -> list[dict]:
    """Merge each decision's intent fields in as `intent` (one query per agent collection)."""
    by_agent: dict[str, list] = {}
    for d in decisions:
        if d.get("agent") and d.get("intent_id") is not None:
            by_agent.setdefault(d["agent"], []).append(d["intent_id"])
    intents: dict = {}
    for agent, ids in by_agent.items():
        if not str(agent).isalnum():
            continue
        async for doc in db[f"intents_{agent}"].find({"_id": {"$in": ids}}, INTENT_FIELDS):
            intents[doc["_id"]] = doc
    for d in decisions:
        d["intent"] = intents.get(d.get("intent_id"))
    return decisions


async def _objects_at(db, tower_id: str, t: datetime) -> list[dict]:
    lo = t - timedelta(seconds=OBJECT_WINDOW_S)
    pipeline = [
        {"$match": {"meta.tower_id": tower_id, "t": {"$gt": lo, "$lte": t}}},
        {"$sort": {"t": 1}},
        {"$group": {"_id": "$meta.object_id", "p": {"$last": "$$ROOT"}}},
    ]
    last, removed = await asyncio.gather(
        db.positions.aggregate(pipeline),
        db.state_events.find(
            {"tower_id": tower_id, "kind": "object_removed", "t": {"$gt": lo, "$lte": t}},
            {"ref": 1, "t": 1, "_id": 0},
        ).to_list(1000),
    )
    gone: dict[str, datetime] = {}
    for ev in removed:
        gone[ev["ref"]] = max(ev["t"], gone.get(ev["ref"], ev["t"]))
    out = []
    async for row in last:
        p = row["p"]
        meta = p.get("meta", {})
        if row["_id"] in gone and gone[row["_id"]] >= p["t"]:
            continue
        out.append(
            {
                "_id": row["_id"],
                "tower_id": meta.get("tower_id"),
                "kind": meta.get("kind"),
                "callsign": meta.get("callsign"),
                "label": None,
                "status": p.get("status"),
                "position": p.get("position"),
                "alt_ft": p.get("alt_ft"),
                "heading_deg": p.get("heading_deg"),
                "speed_kt": p.get("speed_kt"),
                "lane_id": p.get("lane_id"),
                "clearance": None,
                "hold_short": False,
                "updated_at": p["t"],
                "expires_at": p["t"] + timedelta(seconds=OBJECT_TTL_S),
            }
        )
    out.sort(key=lambda o: str(o["_id"]))
    return out


async def _state_at(db, tower_id: str, kind: str, t: datetime) -> list[dict]:
    """Last state_events document per ref up to t (walks the (tower_id, kind, ref, t) index)."""
    pipeline = [
        {"$match": {"tower_id": tower_id, "kind": kind, "t": {"$lte": t}}},
        {"$sort": {"tower_id": 1, "kind": 1, "ref": 1, "t": -1}},
        {"$group": {"_id": "$ref", "doc": {"$first": "$doc"}}},
        {"$sort": {"_id": 1}},
    ]
    cursor = await db.state_events.aggregate(pipeline)
    return [row["doc"] async for row in cursor if row.get("doc")]


async def _pending_at(db, tower_id: str, t: datetime) -> list[dict]:
    """pending_approval decisions with no later decision for the same intent up to t."""
    pend = (
        await db.decisions.find(
            {"tower_id": tower_id, "outcome": "pending_approval", "t": {"$lte": t}}
        )
        .sort("t", -1)
        .limit(PENDING_LOOKBACK)
        .to_list(PENDING_LOOKBACK)
    )
    if not pend:
        return []
    intent_ids = [d["intent_id"] for d in pend if d.get("intent_id") is not None]
    oldest = min(d["t"] for d in pend)
    later = await db.decisions.find(
        {"tower_id": tower_id, "intent_id": {"$in": intent_ids}, "t": {"$gte": oldest, "$lte": t}},
        {"intent_id": 1, "t": 1, "revokes": 1},
    ).to_list(5000)
    pend_ids = {d["_id"] for d in pend}
    settled = {x["revokes"] for x in later if x.get("revokes") in pend_ids}
    out = []
    for d in pend:
        superseded = d["_id"] in settled or any(
            x["_id"] != d["_id"]
            and x["intent_id"] == d.get("intent_id")
            and (x["t"], x["_id"]) > (d["t"], d["_id"])
            for x in later
        )
        if not superseded:
            out.append(d)
    out.reverse()
    return out


async def snapshot(
    db, tower_id: str, t: datetime | None = None, controller_id: str | None = None
) -> dict:
    live = t is None
    at = now_utc() if live else t
    tq = {"tower_id": tower_id, "t": {"$lte": at}}
    if live:
        objects_c = db.objects.find({"tower_id": tower_id}).sort("_id", 1).to_list(2000)
        lanes_c = db.lane_status.find({"tower_id": tower_id}).sort("_id", 1).to_list(500)
        perms_c = db.permissions.find({}).sort("_id", 1).to_list(50)
    else:
        objects_c = _objects_at(db, tower_id, at)
        lanes_c = _state_at(db, tower_id, "lane_status", at)
        perms_c = _state_at(db, tower_id, "permission", at)
    (
        objects,
        lane_status,
        permissions,
        weather,
        pending,
        decisions,
        transmissions,
        me,
    ) = await asyncio.gather(
        objects_c,
        lanes_c,
        perms_c,
        db.weather.find_one(tq, sort=[("t", -1)]),
        _pending_at(db, tower_id, at),
        db.decisions.find(tq).sort("t", -1).limit(50).to_list(50),
        db.transmissions.find(tq).sort("t", -1).limit(100).to_list(100),
        db.controllers.find_one({"_id": controller_id}) if controller_id else _none(),
    )
    decisions.reverse()
    transmissions.reverse()
    await attach_intents(db, decisions + pending)
    if controller_id:
        role = (me or {}).get("role")
        my_streams = set((me or {}).get("streams", []))
        for ls in lane_status:
            ls["mine"] = ls.get("controller_id") == controller_id
        for d in pending:
            d["mine"] = d.get("controller_id") == controller_id or role == "supervisor"
        for tx in transmissions:
            tx["mine"] = (
                tx.get("controller_id") == controller_id or tx.get("stream_id") in my_streams
            )
    return {
        "t": at,
        "live": live,
        "objects": objects,
        "lane_status": lane_status,
        "permissions": permissions,
        "weather": weather,
        "pending": pending,
        "decisions": decisions,
        "transmissions": transmissions,
    }


async def _none():
    return None


# ---------------------------------------------------------------- tracks / timeline / clips


async def tracks(db, tower_id: str, t_from: datetime, t_to: datetime, step_s: float = 1) -> dict:
    if t_to - t_from > timedelta(seconds=MAX_TRACK_WINDOW_S):
        t_from = t_to - timedelta(seconds=MAX_TRACK_WINDOW_S)
    span = max((t_to - t_from).total_seconds(), 0)
    step = max(int(round(step_s)), 1, -(-int(span) // MAX_TRACK_POINTS))
    pipeline = [
        {"$match": {"meta.tower_id": tower_id, "t": {"$gte": t_from, "$lte": t_to}}},
        {
            "$project": {
                "_id": 0,
                "t": 1,
                "meta.object_id": 1,
                "meta.kind": 1,
                "meta.callsign": 1,
                "c": "$position.coordinates",
                "alt_ft": 1,
                "heading_deg": 1,
            }
        },
        {"$sort": {"t": 1}},
        {
            "$group": {
                "_id": {
                    "o": "$meta.object_id",
                    "b": {"$dateTrunc": {"date": "$t", "unit": "second", "binSize": step}},
                },
                "p": {"$first": "$$ROOT"},
            }
        },
        {"$sort": {"p.t": 1}},
    ]
    objects: dict[str, dict] = {}
    cursor = await db.positions.aggregate(pipeline)
    async for row in cursor:
        p = row["p"]
        meta = p.get("meta", {})
        entry = objects.setdefault(
            str(row["_id"]["o"]),
            {"kind": meta.get("kind"), "callsign": meta.get("callsign"), "points": []},
        )
        c = p.get("c") or [None, None]
        entry["points"].append([p["t"], c[0], c[1], p.get("alt_ft"), p.get("heading_deg")])
    return {"from": t_from, "to": t_to, "step_s": step, "objects": objects}


def _short(text: str | None, n: int = 90) -> str:
    text = (text or "").strip()
    return text if len(text) <= n else text[: n - 1] + "…"


async def timeline(db, tower_id: str, t_from: datetime, t_to: datetime) -> dict:
    rng = {"$gte": t_from, "$lte": t_to}
    q = {"tower_id": tower_id, "t": rng}
    txs, decs, perms, runs = await asyncio.gather(
        db.transmissions.find(
            q,
            {
                "t": 1,
                "stream_id": 1,
                "from": 1,
                "to": 1,
                "transcript": 1,
                "flag": 1,
                "synthetic": 1,
            },
        )
        .sort("t", 1)
        .to_list(MAX_MARKS),
        db.decisions.find(
            q,
            {
                "t": 1,
                "stream_id": 1,
                "severity": 1,
                "outcome": 1,
                "agent": 1,
                "intent_action": 1,
                "rule": 1,
                "text": 1,
                "lane_id": 1,
                "controller_id": 1,
            },
        )
        .sort("t", 1)
        .to_list(MAX_MARKS),
        db.state_events.find(
            {"tower_id": tower_id, "kind": "permission", "t": rng},
            {"t": 1, "ref": 1, "by": 1, "doc.enabled": 1, "doc.mode": 1, "doc.tier": 1},
        )
        .sort("t", 1)
        .to_list(MAX_MARKS),
        db.sim_runs.find(
            {
                "tower_id": tower_id,
                "started_at": {"$lte": t_to},
                "$or": [{"ended_at": None}, {"ended_at": {"$gte": t_from}}],
            },
        )
        .sort("started_at", 1)
        .to_list(200),
    )
    marks: list[dict] = []
    for tx in txs:
        who = f"{tx.get('from') or '?'}→{tx.get('to') or '?'}"
        marks.append(
            {
                "t": tx["t"],
                "type": "transmission",
                "stream_id": tx.get("stream_id"),
                "severity": "caution" if tx.get("flag") else None,
                "label": _short(f"{who}: {tx.get('transcript') or ''}"),
                "id": tx["_id"],
                "synthetic": bool(tx.get("synthetic")),
            }
        )
    for d in decs:
        is_alert = d.get("intent_action") in ("warn", "stop") and (
            d.get("severity") in ALERT_SEVERITIES or d.get("outcome") == "executed"
        )
        head = f"{d.get('agent')} {d.get('intent_action')} {d.get('outcome')} ({d.get('rule')})"
        marks.append(
            {
                "t": d["t"],
                "type": "alert" if is_alert else "decision",
                "stream_id": d.get("stream_id"),
                "severity": d.get("severity"),
                "label": _short(f"{head}: {d.get('text') or ''}"),
                "id": d["_id"],
                "outcome": d.get("outcome"),
                "lane_id": d.get("lane_id"),
                "controller_id": d.get("controller_id"),
            }
        )
    for ev in perms:
        doc = ev.get("doc") or {}
        state = "enabled" if doc.get("enabled", True) else "disabled"
        label = f"{ev.get('ref')}: {state}, {doc.get('mode')}, {doc.get('tier')}"
        if ev.get("by"):
            label += f" (by {ev['by']})"
        marks.append(
            {
                "t": ev["t"],
                "type": "permission",
                "stream_id": None,
                "severity": None,
                "label": label,
                "id": ev["_id"],
            }
        )
    for run in runs:
        name = f"{run.get('scenario')} ({run.get('kind')})"
        if run["started_at"] >= t_from:
            marks.append(
                {
                    "t": run["started_at"],
                    "type": "run",
                    "stream_id": None,
                    "severity": "warning" if run.get("kind") == "incident" else None,
                    "label": _short(f"run start: {name} {run.get('note') or ''}"),
                    "id": run["_id"],
                }
            )
        if run.get("ended_at") and run["ended_at"] <= t_to:
            marks.append(
                {
                    "t": run["ended_at"],
                    "type": "run",
                    "stream_id": None,
                    "severity": None,
                    "label": f"run end: {name}",
                    "id": run["_id"],
                }
            )
    marks.sort(key=lambda m: m["t"])
    return {"marks": marks}


def clip_view(clip: dict, transmission: dict | None = None) -> dict:
    return {
        "clip_id": clip["_id"],
        "stream_id": clip.get("stream_id"),
        "t_start": clip.get("t_start"),
        "t_end": clip.get("t_end"),
        "duration_s": clip.get("duration_s"),
        "direction": clip.get("direction"),
        "has_audio": clip.get("audio_id") is not None,
        "status": clip.get("status"),
        "speaker": clip.get("speaker"),
        "transmission": transmission,
    }


async def stream_clips(db, stream_id: str, t_from: datetime, t_to: datetime) -> dict:
    clips = (
        await db.audio_clips.find(
            {"stream_id": stream_id, "t_start": {"$gte": t_from, "$lte": t_to}}
        )
        .sort("t_start", 1)
        .limit(MAX_CLIPS)
        .to_list(MAX_CLIPS)
    )
    # transmissions are joined by _id (audio_clips.transmission_id): no extra index needed
    tx_ids = [c["transmission_id"] for c in clips if c.get("transmission_id")]
    tx_by_id: dict = {}
    if tx_ids:
        async for tx in db.transmissions.find({"_id": {"$in": tx_ids}}):
            tx_by_id[tx["_id"]] = tx
    return {"clips": [clip_view(c, tx_by_id.get(c.get("transmission_id"))) for c in clips]}
