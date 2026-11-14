"""Audit agent on the v2 log: one deterministic timeline aggregation and a grounded narrative.

``timeline(db, tower_id, ...)`` runs ONE aggregation on ``transmissions`` that ``$unionWith``s
the other insert-only logs: ``intents_*``, ``decisions``, ``state_events`` and
``security_events``. Each item is::

    {t, kind, actor, summary, lane_id, controller_id, rule, authorized_by, approved_by, doc_id}

``kind`` is one of KINDS. Order is deterministic: ``t``, then KINDS order, then ``doc_id``.
``explain(db, question)`` picks the entity / lane / controller / time from the question, builds
the timeline and writes a short narrative that cites ``[doc_id]``s from it only.

Read-only: this module only calls ``aggregate``, ``find``, ``find_one`` and ``distinct``.
"""

from __future__ import annotations

import logging
import os
import re
from datetime import UTC, datetime, timedelta
from typing import Any

from bson import ObjectId
from pymongo.database import Database

from holdshort.contracts import AGENTS_WITH_INTENTS

log = logging.getLogger("holdshort.audit")

KINDS = ("transmission", "intent", "decision", "lane_status", "permission", "rule_set",
         "controller", "object_removed", "security_event")
GLOBAL_STATE = ("permission", "rule_set", "controller")
DEFAULT_WINDOW = timedelta(minutes=15)
DEFAULT_TOWER = "LGA"
DEFAULT_MODEL = "claude-opus-5-5"
INTENT_COLLS = [f"intents_{a}" for a in AGENTS_WITH_INTENTS]
_CITE_RE = re.compile(r"\[([0-9a-f]{24})\]")


# ------------------------------------------------------------------------ pipeline


def _common(kind_expr: Any, order_expr: Any, actor: Any, lane: Any, ctl: Any, rule: Any,
            auth: Any, appr: Any, raw: dict) -> dict:
    return {"$project": {
        "_id": 0, "t": 1, "kind": kind_expr, "kind_order": order_expr,
        "actor": actor, "lane_id": lane, "controller_id": ctl, "rule": rule,
        "authorized_by": auth, "approved_by": appr,
        "doc_id": {"$toString": "$_id"}, "raw": raw,
    }}


_NULL = {"$literal": None}


def _and(*conds: dict | None) -> dict:
    cs = [c for c in conds if c]
    return {"$and": cs} if cs else {}


def _time(lo: datetime | None, hi: datetime | None) -> dict | None:
    return {"t": {"$gte": lo, "$lte": hi}} if lo is not None else None


def _transmissions(tower_id, names, lane_id, ctl_lanes, controller_id, lo, hi) -> list[dict]:
    match = _and(
        {"tower_id": tower_id}, _time(lo, hi),
        {"$or": [{"from": {"$in": names}}, {"to": {"$in": names}}]} if names else None,
        {"lane_id": lane_id} if lane_id else None,
        {"$or": [{"controller_id": controller_id}, {"lane_id": {"$in": ctl_lanes}}]}
        if controller_id else None,
    )
    return [{"$match": match}, _common(
        {"$literal": "transmission"}, {"$literal": KINDS.index("transmission")},
        {"$cond": [{"$eq": ["$synthetic", True]}, "HOLDSHORT", "$from"]},
        "$lane_id", "$controller_id", _NULL, _NULL, _NULL,
        {"from": "$from", "to": "$to", "kind": "$kind", "instruction": "$instruction",
         "runway": "$runway", "transcript": "$transcript", "readback": "$readback",
         "stream_id": "$stream_id", "synthetic": "$synthetic", "decision_id": "$decision_id",
         "flag": "$flag", "confidence": "$confidence"},
    )]


def _intents(tower_id, names, lane_id, ctl_lanes, controller_id, lo, hi) -> list[dict]:
    match = _and(
        {"tower_id": tower_id}, _time(lo, hi),
        {"$or": [{"target": {"$in": names}}, {"conflicts_with": {"$in": names}},
                 {"object_ids": {"$in": names}}, {"agent": {"$in": names}}]}
        if names else None,
        {"lane_id": lane_id} if lane_id else None,
        {"lane_id": {"$in": ctl_lanes}} if controller_id else None,
    )
    return [{"$match": match}, _common(
        {"$literal": "intent"}, {"$literal": KINDS.index("intent")}, "$agent",
        "$lane_id", _NULL, "$reason", _NULL, _NULL,
        {"agent": "$agent", "action": "$action", "severity": "$severity", "target": "$target",
         "conflicts_with": "$conflicts_with", "proposal_text": "$proposal_text",
         "detail": "$detail"},
    )]


def _decisions(tower_id, names, lane_id, controller_id, lo, hi) -> list[dict]:
    lookups = [{"$lookup": {"from": c, "localField": "intent_id", "foreignField": "_id",
                            "as": f"_i{n}"}} for n, c in enumerate(INTENT_COLLS)]
    match = _and(
        {"$or": [{"target": {"$in": names}}, {"notified": {"$in": names}},
                 {"agent": {"$in": names}}, {"_intent.target": {"$in": names}},
                 {"_intent.conflicts_with": {"$in": names}}, {"approved_by": {"$in": names}}]}
        if names else None,
        {"lane_id": lane_id} if lane_id else None,
        {"$or": [{"controller_id": controller_id}, {"approved_by": controller_id}]}
        if controller_id else None,
    )
    return [
        {"$match": _and({"tower_id": tower_id}, _time(lo, hi))},
        *lookups,
        {"$set": {"_intent": {"$first": {"$concatArrays": [
            f"$_i{n}" for n in range(len(INTENT_COLLS))]}}}},
        {"$match": match},
        _common(
            {"$literal": "decision"}, {"$literal": KINDS.index("decision")},
            {"$ifNull": ["$approved_by", "gate"]}, "$lane_id", "$controller_id", "$rule",
            "$authorized_by", "$approved_by",
            {"agent": "$agent", "action": "$intent_action", "outcome": "$outcome",
             "severity": "$severity", "target": "$target", "text": "$text",
             "intent_id": {"$toString": "$intent_id"}, "revokes": {"$toString": "$revokes"},
             "notified": "$notified", "mode": "$mode", "stream_id": "$stream_id",
             "conflicts_with": "$_intent.conflicts_with", "intent_reason": "$_intent.reason"},
        ),
    ]


def _state_events(tower_id, names, lane_id, controller_id, lo, hi) -> list[dict]:
    is_global = {"kind": {"$in": list(GLOBAL_STATE)}}
    ent = None
    if names:
        ent = {"$or": [{"ref": {"$in": names}}, {"doc.occupants": {"$in": names}},
                       {"doc.reservations.callsign": {"$in": names}}]}
    lane = {"$or": [{"kind": "lane_status", "ref": lane_id}, is_global]} if lane_id else None
    ctl = None
    if controller_id:
        ctl = {"$or": [{"by": controller_id}, {"ref": controller_id},
                       {"doc.controller_id": controller_id}]}
    return [{"$match": _and({"tower_id": tower_id}, _time(lo, hi), ent, lane, ctl)}, _common(
        "$kind", {"$indexOfArray": [list(KINDS), "$kind"]}, "$by",
        {"$cond": [{"$eq": ["$kind", "lane_status"]}, "$ref", None]},
        {"$ifNull": ["$doc.controller_id", None]},
        {"$cond": [{"$eq": ["$kind", "rule_set"]},
                   {"$ifNull": ["$doc.version", "$ref"]}, None]},
        _NULL, _NULL,
        {"ref": "$ref", "by": "$by", "state": "$doc.state", "reason": "$doc.reason",
         "occupants": "$doc.occupants", "reservations": "$doc.reservations.callsign",
         "enabled": "$doc.enabled", "mode": "$doc.mode", "tier": "$doc.tier",
         "version": "$doc.version", "active": "$doc.active", "lanes": "$doc.lanes"},
    )]


def _security_events(names, lane_id, controller_id, lo, hi) -> list[dict]:
    # security_events carry no tower_id or lane_id; with a lane filter they are left out.
    if lane_id:
        return [{"$match": {"_id": None}}]
    who = names + ([controller_id] if controller_id else [])
    match = _and(_time(lo, hi),
                 {"$or": [{"agent": {"$in": who}}, {"controller_id": {"$in": who}}]}
                 if who and (names or controller_id) else None)
    return [{"$match": match}, _common(
        {"$literal": "security_event"}, {"$literal": KINDS.index("security_event")},
        {"$ifNull": ["$controller_id", "$agent"]}, _NULL,
        {"$ifNull": ["$controller_id", None]}, "$kind", _NULL, _NULL,
        {"kind": "$kind", "attempted": "$attempted", "error_code": "$error_code",
         "error": "$error"},
    )]


def build_pipeline(tower_id: str, names: list[str], lane_id: str | None,
                   controller_id: str | None, ctl_lanes: list[str],
                   lo: datetime | None, hi: datetime | None) -> list[dict]:
    """The single aggregation (run on ``transmissions``) behind ``timeline()``."""
    stages = _transmissions(tower_id, names, lane_id, ctl_lanes, controller_id, lo, hi)
    for coll in INTENT_COLLS:
        stages.append({"$unionWith": {"coll": coll, "pipeline": _intents(
            tower_id, names, lane_id, ctl_lanes, controller_id, lo, hi)}})
    stages.append({"$unionWith": {"coll": "decisions", "pipeline": _decisions(
        tower_id, names, lane_id, controller_id, lo, hi)}})
    stages.append({"$unionWith": {"coll": "state_events", "pipeline": _state_events(
        tower_id, names, lane_id, controller_id, lo, hi)}})
    stages.append({"$unionWith": {"coll": "security_events", "pipeline": _security_events(
        names, lane_id, controller_id, lo, hi)}})
    stages.append({"$sort": {"t": 1, "kind_order": 1, "doc_id": 1}})
    stages.append({"$unset": "kind_order"})
    return stages


# ------------------------------------------------------------------------ summaries


def _j(xs: Any) -> str:
    return ",".join(str(x) for x in xs or []) or "-"


def _summary(kind: str, r: dict, it: dict) -> str:
    if kind == "transmission":
        who = "Phylax Tower (synthetic) " if r.get("synthetic") else ""
        s = (f"{who}{r.get('from')} -> {r.get('to')} on {r.get('stream_id')}: "
             f"{r.get('kind')}/{r.get('instruction')}")
        if r.get("runway"):
            s += f" runway {r['runway']}"
        s += f' "{r.get("transcript", "")}" readback={r.get("readback")}'
        if r.get("flag"):
            s += f" flag={r['flag']}"
        if r.get("decision_id"):
            s += f" (for decision {r['decision_id']})"
        return s
    if kind == "intent":
        s = (f"{r.get('agent')} agent asked to {r.get('action')} ({r.get('severity')}) "
             f"for {_j(r.get('target'))} reason {it.get('rule')}")
        if r.get("conflicts_with"):
            s += f", conflicts with {r['conflicts_with']}"
        if r.get("proposal_text"):
            s += f', proposal "{r["proposal_text"]}"'
        if r.get("detail"):
            s += f": {r['detail']}"
        return s
    if kind == "decision":
        ab = it.get("authorized_by") or {}
        s = (f"gate {r.get('outcome')} {r.get('agent')} {r.get('action')} for "
             f"{_j(r.get('target'))} (intent {r.get('intent_id')}), rule {it.get('rule')}, "
             f"routed to {it.get('controller_id')}, authorized by permission "
             f"{ab.get('permission_id')} rule set {ab.get('rule_set_version')}")
        if it.get("approved_by"):
            s += f", approved by {it['approved_by']}"
        if r.get("revokes"):
            s += f", revokes decision {r['revokes']}"
        if r.get("text"):
            s += f': "{r["text"]}"'
        return s
    if kind == "lane_status":
        s = (f"lane {r.get('ref')} -> {r.get('state')} occupants {_j(r.get('occupants'))} "
             f"reservations {_j(r.get('reservations'))}")
        return s + (f" ({r['reason']})" if r.get("reason") else "")
    if kind == "permission":
        return (f"permission {r.get('ref')} set enabled={r.get('enabled')} mode={r.get('mode')}"
                f" tier={r.get('tier')} by {r.get('by')}")
    if kind == "rule_set":
        return f"rule set {r.get('version') or r.get('ref')} active={r.get('active')} by " \
               f"{r.get('by')}"
    if kind == "controller":
        return f"controller {r.get('ref')} lanes {_j(r.get('lanes'))} by {r.get('by')}"
    if kind == "object_removed":
        return f"object {r.get('ref')} removed"
    if kind == "security_event":
        s = f"security event {r.get('kind')}: {it.get('actor')} attempted {r.get('attempted')}"
        if r.get("error_code") is not None:
            s += f" -> {r['error_code']}"
        return s + (f" ({r['error']})" if r.get("error") else "")
    return kind


def _plain(v: Any) -> Any:
    if isinstance(v, ObjectId):
        return str(v)
    if isinstance(v, dict):
        return {k: _plain(x) for k, x in v.items()}
    if isinstance(v, list):
        return [_plain(x) for x in v]
    if isinstance(v, datetime) and v.tzinfo is None:
        return v.replace(tzinfo=UTC)
    return v


def _parse_time(v: datetime | str | None) -> datetime | None:
    if v is None or v == "":
        return None
    if isinstance(v, str):
        v = datetime.fromisoformat(v.replace("Z", "+00:00"))
    return v if v.tzinfo else v.replace(tzinfo=UTC)


def _related_names(db: Database, tower_id: str, entity: str) -> list[str]:
    """The entity plus every callsign an intent about it conflicts with (and vice versa)."""
    names = {entity, entity.lower()}
    for coll in INTENT_COLLS:
        for d in db[coll].find({"tower_id": tower_id, "target": entity},
                               {"conflicts_with": 1}):
            if d.get("conflicts_with"):
                names.add(d["conflicts_with"])
    return sorted(names)


def timeline(db: Database, tower_id: str = DEFAULT_TOWER, *, entity: str | None = None,
             lane_id: str | None = None, controller_id: str | None = None,
             around: datetime | str | None = None,
             window: timedelta = DEFAULT_WINDOW) -> list[dict]:
    """Everything the insert-only logs hold, filtered, in deterministic order.

    ``entity``: a callsign (its conflicting callsigns are included), an agent or controller id.
    ``around``: only items within ``window`` of it. With no filter at all, ``around`` = now.
    """
    names = _related_names(db, tower_id, entity.strip().upper()) if entity else []
    if entity and entity.strip() != entity.strip().upper():
        names.append(entity.strip())
    around = _parse_time(around)
    if around is None and not (names or lane_id or controller_id):
        around = datetime.now(UTC)
    lo, hi = (around - window, around + window) if around else (None, None)
    ctl_lanes: list[str] = []
    if controller_id:
        c = db.controllers.find_one({"_id": controller_id}, {"lanes": 1}) or {}
        ctl_lanes = list(c.get("lanes") or [])
    pipe = build_pipeline(tower_id, names, lane_id, controller_id, ctl_lanes, lo, hi)
    out = []
    for d in db.transmissions.aggregate(pipe):
        d = _plain(d)
        raw = d.pop("raw", {}) or {}
        item = {k: d.get(k) for k in ("t", "kind", "actor", "summary", "lane_id",
                                       "controller_id", "rule", "authorized_by",
                                       "approved_by", "doc_id")}
        item["summary"] = _summary(item["kind"], raw, item)
        out.append(item)
    return out


# ------------------------------------------------------------------------ question parsing

_NUM_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "ten": 10,
              "fifteen": 15, "twenty": 20, "thirty": 30, "a": 1}
_LANE_WORD = re.compile(r"\b(runway|rwy|taxiway|twy|apron|ramp)\s+([0-9]{1,2}[LRC]?|[A-Z]{1,2})?\b",
                        re.I)
_LAST = re.compile(r"\blast\s+(\d+|[a-z]+)?\s*(second|sec|minute|min|hour)s?\b", re.I)


def _known_callsigns(db: Database, tower_id: str) -> set[str]:
    q = {"tower_id": tower_id}
    names = set(db.transmissions.distinct("from", q)) | set(db.transmissions.distinct("to", q))
    for coll in INTENT_COLLS:
        names |= set(db[coll].distinct("target", q))
    return {n for n in names if isinstance(n, str)} - {"TOWER", "GROUND", "OPS", "UNKNOWN",
                                                        "HOLDSHORT"}


def parse_question(db: Database, question: str, tower_id: str = DEFAULT_TOWER,
                   now: datetime | None = None) -> dict:
    """Pick {entity, lane_id, controller_id, around, window} from the question and the data."""
    from holdshort.world import lane_for_designator

    tokens = re.findall(r"[A-Za-z0-9_]+", question)
    known = _known_callsigns(db, tower_id)
    entity = next((t.upper() for t in tokens if t.upper() in known), None)
    controllers = {c["_id"] for c in db.controllers.find({"tower_id": tower_id}, {"_id": 1})}
    controller_id = next((t for t in tokens if t in controllers), None)
    lanes = {x["_id"] for x in db.lanes.find({"tower_id": tower_id}, {"_id": 1})}
    lane_id = next((t.upper() for t in tokens if t.upper() in lanes), None)
    if lane_id is None:
        m = _LANE_WORD.search(question)
        if m and m.group(2):
            lane_id = lane_for_designator(db, tower_id, m.group(2).upper())
    around, window = None, DEFAULT_WINDOW
    m = _LAST.search(question)
    if m:
        n = m.group(1) or "1"
        n = int(n) if n.isdigit() else _NUM_WORDS.get(n.lower(), 1)
        unit = m.group(2).lower()
        secs = n * (1 if unit.startswith("s") else 3600 if unit == "hour" else 60)
        around, window = now or datetime.now(UTC), timedelta(seconds=secs)
    elif not (entity or lane_id or controller_id):
        around = now or datetime.now(UTC)
    return {"tower_id": tower_id, "entity": entity, "lane_id": lane_id,
            "controller_id": controller_id, "around": around, "window": window}


# ------------------------------------------------------------------------ narratives


def _cite(it: dict) -> str:
    return f"[{it['doc_id']}]"


def fallback_narrative(question: str, items: list[dict], focus: dict) -> str:
    """Plain rule-based narrative assembled only from the timeline items."""
    if not items:
        what = focus.get("entity") or focus.get("lane_id") or focus.get("controller_id")
        return f"No records found{f' for {what}' if what else ''}."
    q = question.lower()
    ent = focus.get("entity")
    lines: list[str] = []
    decisions = [it for it in items if it["kind"] == "decision"]
    if "approv" in q or "who allowed" in q:
        hits = [d for d in decisions if d.get("approved_by")]
        topic = [w for w in ("cross", "land", "takeoff", "taxi", "hold") if w in q]
        if topic:
            hits = [d for d in hits if any(w in d["summary"].lower() for w in topic)] or hits
        for d in hits:
            lines.append(f"{d['approved_by']} approved it: {d['summary']} {_cite(d)}.")
        if not hits:
            lines.append("No decision in this timeline has an approved_by.")
        return " ".join(lines)
    if any(w in q for w in ("why", "held", "hold", "stop")):
        stops = [d for d in decisions if " stop " in f" {d['summary']} "
                 and (not ent or ent in d["summary"])]
        for d in stops:
            ab = d.get("authorized_by") or {}
            lines.append(
                f"{ent or 'The target'} was stopped by decision {d['doc_id']}: rule {d['rule']}, "
                f"authorized by permission {ab.get('permission_id')} (rule set "
                f"{ab.get('rule_set_version')}), approved_by {d.get('approved_by') or 'none'} "
                f"{_cite(d)}.")
        conflicts = {c for it in items if it["kind"] == "intent"
                     for c in re.findall(r"conflicts with ([A-Z0-9]+)", it["summary"])}
        for it in items:
            if it["kind"] == "transmission" and it["actor"] in conflicts | {"TOWER", "GROUND"} \
                    and any(c in it["summary"] for c in conflicts) \
                    and "clearance/" in it["summary"]:
                lines.append(f"The conflicting clearance: {it['summary']} {_cite(it)}.")
        for it in items:
            if it["kind"] == "lane_status" and "conflict" in it["summary"]:
                lines.append(f"Lane status: {it['summary']} {_cite(it)}.")
        if lines:
            return " ".join(lines)
    for it in items[:20]:
        lines.append(f"{it['t'].isoformat()} {it['summary']} {_cite(it)}.")
    if len(items) > 20:
        lines.append(f"(+{len(items) - 20} more items in the timeline)")
    return "\n".join(lines)


_SYSTEM = (
    "You are the audit agent of an airport tower safety system. Explain what happened using "
    "ONLY the timeline items given. End every factual sentence with the doc_id of the item it "
    "comes from in square brackets, e.g. [65f0c0ffee...]. Name rule, authorized_by and "
    "approved_by exactly as they appear. Never add facts, times, causes, people or callsigns "
    "that are not in the timeline; if it does not answer the question, say so. Under 150 "
    "words, plain text. Put every doc_id you cite in `cited`."
)


def _llm_from_env():
    """The audit chat model from holdshort.llm (task "audit"), or None for provider none."""
    from holdshort import llm as llm_layer

    if llm_layer.provider() == "none":
        return None
    if os.environ.get("LANGSMITH_API_KEY", "").strip():
        os.environ.setdefault("LANGSMITH_TRACING", "true")
        os.environ.setdefault("LANGSMITH_PROJECT", "holdshort-audit")
    return llm_layer.chat_model("audit", AUDIT_TIMEOUT_S)


AUDIT_TIMEOUT_S = 60


def _llm_narrative(llm, question: str, items: list[dict]) -> str | None:
    from pydantic import BaseModel, Field

    class AuditNarrative(BaseModel):
        narrative: str = Field(description="Short narrative citing [doc_id]s")
        cited: list[str] = Field(description="doc_ids cited")

    payload = [{**it, "t": it["t"].isoformat()} for it in items]
    user = f"Question: {question}\nTimeline: {payload}"
    if llm == "env":  # the shared layer: hard timeout, never raises
        from holdshort import llm as llm_layer

        out = llm_layer.structured("audit", _SYSTEM, user, AuditNarrative, AUDIT_TIMEOUT_S)
        if out is None:
            return None
    else:
        try:
            out = llm.with_structured_output(AuditNarrative).invoke([
                ("system", _SYSTEM), ("human", user)])
        except Exception as e:  # noqa: BLE001  any provider failure -> rule-based fallback
            from holdshort.env import redact

            log.warning("audit LLM call failed: %s", redact(e))
            return None
    if isinstance(out, dict):
        out = AuditNarrative(**out)
    ids = {it["doc_id"] for it in items}
    cited = set(_CITE_RE.findall(out.narrative)) | set(out.cited)
    if not cited or not cited <= ids:
        log.warning("audit LLM narrative cited ids outside the timeline; using fallback")
        return None
    return out.narrative


def explain(db: Database, question: str, *, tower_id: str = DEFAULT_TOWER,
            llm: Any = "env", now: datetime | None = None) -> dict:
    """Answer an audit question: ``{timeline, narrative, cited, focus, source}``.

    ``llm`` is a LangChain chat model, None (rule-based only) or "env" (holdshort.llm task
    "audit": the configured provider, or rule-based when it is "none").
    The narrative may only cite doc_ids that are in the timeline.
    """
    focus = parse_question(db, question, tower_id, now)
    items = timeline(db, tower_id, entity=focus["entity"], lane_id=focus["lane_id"],
                     controller_id=focus["controller_id"], around=focus["around"],
                     window=focus["window"])
    if llm == "env":
        from holdshort import llm as llm_layer

        llm = "env" if llm_layer.provider() != "none" else None
    narrative, source = None, "rules"
    if llm is not None and items:
        narrative = _llm_narrative(llm, question, items)
        source = "llm" if narrative else "rules"
    if narrative is None:
        narrative = fallback_narrative(question, items, focus)
    ids = {it["doc_id"] for it in items}
    cited = list(dict.fromkeys(c for c in _CITE_RE.findall(narrative) if c in ids))
    return {"timeline": items, "narrative": narrative, "cited": cited, "focus": focus,
            "source": source}


def _json(v: Any) -> Any:
    if isinstance(v, datetime):
        return v.astimezone(UTC).isoformat().replace("+00:00", "Z")
    if isinstance(v, timedelta):
        return v.total_seconds()
    if isinstance(v, dict):
        return {k: _json(x) for k, x in v.items()}
    if isinstance(v, list):
        return [_json(x) for x in v]
    if isinstance(v, ObjectId):
        return str(v)
    return v


def answer(db: Database, q: str) -> dict:
    """Hook for ``GET /control/audit?q=...``: explain() as JSON-safe data."""
    return _json(explain(db, q))
