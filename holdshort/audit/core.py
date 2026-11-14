"""Audit timeline (one aggregation) and LLM explanation grounded in it.

Timeline items: ``{t, kind, actor, summary, rule, authorized_by, approved_by, doc_id}``.

- ``kind`` is one of KINDS (transmission, intent, decision, permission, security_event).
- ``doc_id`` is the source document's ``_id`` as a string; narratives cite it as
  ``[doc_id]``.
- Order is deterministic: ``t``, then KINDS order, then ``doc_id``.

Read-only: only ``aggregate``/``distinct`` are used.
"""

from __future__ import annotations

import json
import logging
import os
import re
from datetime import UTC, datetime, timedelta
from typing import Any

from bson import ObjectId
from pymongo.database import Database

from holdshort.contracts import AGENTS_WITH_INTENTS, CALLSIGN_PATTERN

log = logging.getLogger("holdshort.audit")

KINDS = ("transmission", "intent", "decision", "permission", "security_event")
DEFAULT_WINDOW = timedelta(minutes=10)
DEFAULT_MODEL = "claude-opus-5-5"
_INTENT_COLLS = [f"intents_{a}" for a in AGENTS_WITH_INTENTS]
_CITE_RE = re.compile(r"\[([0-9a-f]{24})\]")


# ------------------------------------------------------------------ pipeline parts


def _s(expr: Any) -> dict:
    """Null-safe string conversion inside $concat."""
    return {"$ifNull": [{"$toString": expr}, ""]}


def _opt(prefix: str, expr: Any) -> dict:
    """`prefix + expr` when expr is set, else ""."""
    return {"$cond": [{"$ifNull": [expr, False]}, {"$concat": [prefix, _s(expr)]}, ""]}


def _time_match(field: str, lo: datetime | None, hi: datetime | None) -> list[dict]:
    if lo is None:
        return []
    return [{"$match": {field: {"$gte": lo, "$lte": hi}}}]


def _out(kind: str, t, actor, summary, rule=None, authorized_by=None, approved_by=None) -> dict:
    return {
        "$project": {
            "_id": 0,
            "t": t,
            "kind": {"$literal": kind},
            "kind_order": {"$literal": KINDS.index(kind)},
            "actor": actor,
            "summary": summary,
            "rule": rule if rule is not None else {"$literal": None},
            "authorized_by": authorized_by if authorized_by is not None else {"$literal": None},
            "approved_by": approved_by if approved_by is not None else {"$literal": None},
            "doc_id": {"$toString": "$_id"},
        }
    }


def _transmissions(entity: str, lo, hi) -> list[dict]:
    return [
        {"$match": {"$or": [{"from": entity}, {"to": entity}]}},
        *_time_match("t", lo, hi),
        _out(
            "transmission",
            "$t",
            "$from",
            {"$concat": [
                _s("$from"), " -> ", _s("$to"), " (", _s("$kind"), "/", _s("$instruction"),
                _opt(" runway ", "$runway"), _opt(" at ", "$at"), "): \"", _s("$transcript"),
                "\" readback=", _s("$readback"), " verified=", _s("$verified"),
            ]},
        ),
    ]


def _intents(coll: str, entity: str, lo, hi) -> list[dict]:
    return [
        {"$match": {"$or": [{"target": entity}, {"agent": entity.lower()},
                            {"conflicts_with": entity}]}},
        *_time_match("t", lo, hi),
        _out(
            "intent",
            "$t",
            "$agent",
            {"$concat": [
                _s("$agent"), " agent requested ", _s("$action"), " for ",
                {"$reduce": {"input": {"$ifNull": ["$target", []]}, "initialValue": "",
                             "in": {"$concat": ["$$value",
                                                {"$cond": [{"$eq": ["$$value", ""]}, "", ","]},
                                                "$$this"]}}},
                _opt(" runway ", "$runway"), _opt(" (conflicts with ", "$conflicts_with"),
                {"$cond": [{"$ifNull": ["$conflicts_with", False]}, ")", ""]},
                _opt(": ", "$reason"), _opt(" proposal: ", "$proposal_text"),
            ]},
            rule="$reason",
        ),
    ]


def _decisions(entity: str, lo, hi) -> list[dict]:
    lookups: list[dict] = []
    for coll in _INTENT_COLLS:
        lookups.append({"$lookup": {"from": coll, "localField": "intent_id",
                                    "foreignField": "_id", "as": f"_i_{coll}"}})
    return [
        *_time_match("t", lo, hi),
        *lookups,
        {"$set": {"_intent": {"$first": {"$concatArrays": [f"$_i_{c}" for c in _INTENT_COLLS]}}}},
        {"$match": {"$or": [{"_intent.target": entity}, {"notified": entity},
                            {"agent": entity.lower()}, {"_intent.conflicts_with": entity}]}},
        {"$lookup": {"from": "permissions", "localField": "authorized_by.permission_id",
                     "foreignField": "_id", "as": "_perm"}},
        {"$set": {"_perm": {"$first": "$_perm"}}},
        _out(
            "decision",
            "$t",
            {"$literal": "gate"},
            {"$concat": [
                "gate ", _s("$outcome"), " ", _s("$agent"), " ", _s("$intent_action"),
                " (intent ", _s("$intent_id"), ")",
                _opt(" safety reason: ", "$_intent.reason"),
                _opt("; permission agent=", "$_perm.agent"),
                _opt(" tier=", "$_perm.tier"),
                _opt("; approved by ", "$approved_by"),
                _opt("; revokes decision ", "$revokes"),
                {"$cond": [{"$gt": [{"$size": {"$ifNull": ["$notified", []]}}, 0]},
                           {"$concat": ["; notified ", {"$reduce": {
                               "input": "$notified", "initialValue": "",
                               "in": {"$concat": ["$$value", {"$cond": [
                                   {"$eq": ["$$value", ""]}, "", ","]}, "$$this"]}}}]},
                           ""]},
            ]},
            rule="$rule",
            authorized_by="$authorized_by",
            approved_by="$approved_by",
        ),
    ]


def _permissions(entity: str, lo, hi) -> list[dict]:
    # Without a time window: only the entity's own switch (agents) plus the master switch
    # when the entity is an agent. With one: every switch changed inside the window.
    match = (
        _time_match("changed_at", lo, hi)
        if lo is not None
        else [{"$match": {"agent": entity.lower()}}]
    )
    return [
        *match,
        _out(
            "permission",
            "$changed_at",
            "$by",
            {"$concat": ["permission ", _s("$agent"), " enabled=", _s("$enabled"),
                         " tier=", _s("$tier"), " (changed by ", _s("$by"), ")"]},
            rule="$tier",
        ),
    ]


def _security_events(entity: str, lo, hi) -> list[dict]:
    # security_events is written by other tasks; read its common fields defensively.
    t = {"$ifNull": ["$t", "$at"]}
    ent = [{"target": entity}, {"callsign": entity}, {"actor": entity},
           {"agent": entity.lower()}]
    match: dict = {"$or": ent}
    if lo is not None:
        match = {"$or": [*ent, {"t": {"$gte": lo, "$lte": hi}}]}
    return [
        {"$match": match},
        _out(
            "security_event",
            t,
            {"$ifNull": ["$actor", {"$ifNull": ["$agent", "$by"]}]},
            {"$ifNull": ["$summary", {"$ifNull": ["$message", {"$ifNull": [
                "$description", {"$concat": ["security event ", _s("$type")]}]}]}]},
            rule={"$ifNull": ["$rule", {"$ifNull": ["$code", "$type"]}]},
        ),
    ]


def build_pipeline(entity: str, around: datetime | None = None,
                   window: timedelta = DEFAULT_WINDOW) -> list[dict]:
    """The aggregation (run on transmissions) that produces the timeline."""
    lo = hi = None
    if around is not None:
        lo, hi = around - window, around + window
    stages = _transmissions(entity, lo, hi)
    for coll in _INTENT_COLLS:
        stages.append({"$unionWith": {"coll": coll, "pipeline": _intents(coll, entity, lo, hi)}})
    stages.append({"$unionWith": {"coll": "decisions", "pipeline": _decisions(entity, lo, hi)}})
    stages.append({"$unionWith": {"coll": "permissions",
                                  "pipeline": _permissions(entity, lo, hi)}})
    stages.append({"$unionWith": {"coll": "security_events",
                                  "pipeline": _security_events(entity, lo, hi)}})
    stages.append({"$sort": {"t": 1, "kind_order": 1, "doc_id": 1}})
    stages.append({"$unset": "kind_order"})
    return stages


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


def _parse_time(around: datetime | str | None) -> datetime | None:
    if around is None or around == "":
        return None
    if isinstance(around, str):
        around = datetime.fromisoformat(around.replace("Z", "+00:00"))
    return around if around.tzinfo else around.replace(tzinfo=UTC)


def _db(db: Database | None) -> Database:
    if db is not None:
        return db
    from holdshort.db import get_db

    return get_db()


def timeline(entity: str, around: datetime | str | None = None, *, db: Database | None = None,
             window: timedelta = DEFAULT_WINDOW) -> list[dict]:
    """Everything recorded about ``entity`` (callsign or agent), optionally ±window of around."""
    entity = entity.strip().upper()
    pipeline = build_pipeline(entity, _parse_time(around), window)
    return [_plain(d) for d in _db(db).transmissions.aggregate(pipeline)]


# --------------------------------------------------------------------------- explain


def _known_entities(db: Database) -> list[str]:
    names = set(db.transmissions.distinct("from")) | set(db.transmissions.distinct("to"))
    for coll in _INTENT_COLLS:
        names |= set(db[coll].distinct("target"))
    return sorted(n for n in names if isinstance(n, str))


def _heuristic_entity(question: str, known: list[str]) -> str | None:
    tokens = re.findall(r"[A-Za-z0-9]+", question)
    upper = [t.upper() for t in tokens]
    for tok in upper:
        if tok in known and tok != "TOWER":
            return tok
    for tok in upper:
        if tok in known:
            return tok
    for tok in tokens:  # an uppercase callsign-looking token with a digit
        if re.fullmatch(CALLSIGN_PATTERN, tok) and re.search(r"\d", tok):
            return tok
    return None


def _llm_client():
    key = os.environ.get("LLM_API_KEY", "").strip()
    if not key:
        return None
    import anthropic

    return anthropic.Anthropic(api_key=key, timeout=60.0, max_retries=1)


def _model() -> str:
    return os.environ.get("LLM_MODEL", "").strip() or DEFAULT_MODEL


def _llm_json(client, system: str, user: str, schema: dict) -> dict | None:
    import anthropic

    try:
        msg = client.messages.create(
            model=_model(),
            max_tokens=4000,
            system=system,
            output_config={"effort": "low", "format": {"type": "json_schema", "schema": schema}},
            messages=[{"role": "user", "content": user}],
        )
    except anthropic.APIError as e:
        from holdshort.env import redact

        log.warning("LLM call failed: %s", redact(e))
        return None
    if msg.stop_reason != "end_turn":
        log.warning("LLM stopped with %s", msg.stop_reason)
        return None
    text = "".join(b.text for b in msg.content if b.type == "text")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        log.warning("LLM returned non-JSON output")
        return None


_PICK_SCHEMA = {
    "type": "object",
    "properties": {
        "entity": {"type": "string", "description": "Callsign or agent name, or \"\"."},
        "around": {"type": "string", "description": "ISO-8601 UTC time, or \"\"."},
    },
    "required": ["entity", "around"],
    "additionalProperties": False,
}

_NARRATIVE_SCHEMA = {
    "type": "object",
    "properties": {"narrative": {"type": "string"}},
    "required": ["narrative"],
    "additionalProperties": False,
}

_NARRATIVE_SYSTEM = (
    "You are the audit agent for an airport tower safety system. You explain what happened "
    "using ONLY the timeline items you are given. Every sentence that states a fact must end "
    "with the doc_id of the item it comes from in square brackets, e.g. [65f0c0ffee...]. "
    "Name the rule, authorized_by (permission_id and rule_set_version) and approved_by of "
    "the relevant decisions exactly as they appear. Do not add facts, times, causes, people "
    "or callsigns that are not in the timeline. If the timeline does not answer the question, "
    "say so. Keep it under 150 words, plain text."
)


def _pick(question: str, db: Database, client) -> tuple[str | None, datetime | None]:
    known = _known_entities(db)
    entity, around = None, None
    if client is not None:
        out = _llm_json(
            client,
            "Pick the single entity (a callsign such as TRUCK1, or an agent name such as "
            "safety) that the audit question is about, and a scenario time if the question "
            "names one. Use \"\" when absent. Prefer entities from the known list.",
            json.dumps({"question": question, "known_entities": known}),
            _PICK_SCHEMA,
        )
        if out:
            entity = (out.get("entity") or "").strip().upper() or None
            try:
                around = _parse_time(out.get("around") or None)
            except ValueError:
                around = None
    if entity is None or not re.fullmatch(CALLSIGN_PATTERN, entity):
        entity = _heuristic_entity(question, known)
    return entity, around


def _fallback_narrative(entity: str | None, items: list[dict]) -> str:
    if not entity:
        return "I could not tell which callsign or agent the question is about."
    if not items:
        return f"No records found for {entity}."
    lines = []
    for it in items:
        extra = ""
        if it["kind"] == "decision":
            ab = it.get("authorized_by") or {}
            extra = (f" rule={it.get('rule')}; authorized_by permission "
                     f"{ab.get('permission_id')} rule set {ab.get('rule_set_version')}; "
                     f"approved_by={it.get('approved_by') or 'none'}.")
        lines.append(f"{it['t'].isoformat()} {it['summary']}.{extra} [{it['doc_id']}]")
    return "\n".join(lines)


def _grounded(narrative: str, items: list[dict]) -> bool:
    """Every citation must be a timeline doc_id, and there must be at least one."""
    cited = _CITE_RE.findall(narrative)
    ids = {it["doc_id"] for it in items}
    return bool(cited) and all(c in ids for c in cited)


def explain(question: str, *, db: Database | None = None, client: Any = "env") -> dict:
    """Answer an audit question: ``{entity, around, timeline, narrative, source}``.

    ``client`` is an Anthropic client, None (no LLM), or "env" (use LLM_API_KEY if set).
    ``source`` is "llm" when the narrative came from the model and passed the citation
    check, else "deterministic". The timeline is always built by ``timeline()``.
    """
    db = _db(db)
    if client == "env":
        client = _llm_client()
    entity, around = _pick(question, db, client)
    items = timeline(entity, around, db=db) if entity else []
    narrative, source = None, "deterministic"
    if client is not None and entity and items:
        payload = [{**it, "t": it["t"].isoformat()} for it in items]
        out = _llm_json(
            client,
            _NARRATIVE_SYSTEM,
            json.dumps({"question": question, "entity": entity, "timeline": payload},
                       default=str),
            _NARRATIVE_SCHEMA,
        )
        cand = (out or {}).get("narrative", "")
        if cand and _grounded(cand, items):
            narrative, source = cand, "llm"
        elif cand:
            log.warning("LLM narrative cited ids outside the timeline; using fallback")
    if narrative is None:
        narrative = _fallback_narrative(entity, items)
    return {"entity": entity, "around": around, "timeline": items, "narrative": narrative,
            "source": source}
