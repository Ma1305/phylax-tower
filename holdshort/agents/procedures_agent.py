"""Procedures agent (the advisor): propose what the owning controller could say next.

    uv run python -m holdshort.agents.procedures_agent [--db NAME]
    uv run python -m holdshort.agents.procedures_agent rogue [--db NAME]

Trigger: a change stream on ``decisions`` (inserts by the gate):

(a) an executed safety ``stop`` -> propose how to resume, e.g.
    "Truck one, cross runway zero four at Delta after the landing Air Canada eight six
    four six has vacated" (a proposal: the controller must approve it);
(b) an executed safety ``warn`` for ``readback_mismatch`` or about an "unclear - say
    again" call -> propose the corrective call ("Air Canada eight six four six, say again").

Each trigger runs a LangGraph graph ``gather -> draft -> check -> emit`` checkpointed in
MongoDB (langgraph-checkpoint-mongodb, ``thread_id`` = the triggering decision id): a
killed agent resumes the thread where it stopped and a finished thread is never run
again. The proposal's ``_id`` is the triggering decision's id, so even a crash between the
insert and the checkpoint cannot produce a second proposal. LangSmith tracing is on only
when ``LANGSMITH_API_KEY`` is set.

- gather: ``holdshort.memory.query.brief()`` when that package imports, else ``context``,
  ``objects`` and ``lane_status`` directly; every read goes through
  ``scoped_db(db, "procedures")``.
- draft: holdshort.llm task 'advisor' (LLM_PROVIDER); with provider none, a template.
- check: a deterministic guard that rejects any draft granting a new clearance unless it
  is conditional on the conflicting traffic having passed / vacated, and never while
  that traffic still holds a land / takeoff reservation on a lane in conflict. A rejected
  draft falls back to the template.
- emit: a ``propose_instruction`` into intents_procedures (proactive mode), or a ``warn``
  with the same detail (reactive mode). The mode is read from ``permissions`` every time.

``rogue()`` plays a misbehaving procedures agent for the demo: it tries to insert a
"clear" intent, to write an executed decision directly, and to read ``transmissions``
through its scoped handle. All three are blocked and recorded in ``security_events``.
"""

from __future__ import annotations

import argparse
import logging
import os
import re
import sys
from collections.abc import Callable
from typing import Any, TypedDict

from bson import ObjectId
from pymongo import DESCENDING
from pymongo.database import Database
from pymongo.errors import DuplicateKeyError, PyMongoError, WriteError

from holdshort.access import AccessDenied, scoped_db
from holdshort.agents._stream import StreamAgent, now
from holdshort.agents._stream import main as run_agent
from holdshort.contracts import TOWER_ID, Intent, SecurityEvent, to_mongo
from holdshort.db import get_client
from holdshort.env import mongodb_db_name, redact

log = logging.getLogger("holdshort.agents.procedures")

AGENT = "procedures"
DEFAULT_MODEL = "claude-sonnet-5-5"
UNCLEAR_FLAG = "unclear"
READBACK_MISMATCH = "readback_mismatch"

# LangSmith: trace only when a key is configured.
if os.environ.get("LANGSMITH_API_KEY", "").strip():
    os.environ.setdefault("LANGSMITH_TRACING", "true")
else:
    os.environ["LANGSMITH_TRACING"] = "false"

# --------------------------------------------------------------- phraseology

_DIGITS = ["zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "niner"]
_PHONETIC = {
    "A": "Alpha", "B": "Bravo", "C": "Charlie", "D": "Delta", "E": "Echo", "F": "Foxtrot",
    "G": "Golf", "H": "Hotel", "I": "India", "J": "Juliett", "K": "Kilo", "L": "Lima",
    "M": "Mike", "N": "November", "O": "Oscar", "P": "Papa", "Q": "Quebec", "R": "Romeo",
    "S": "Sierra", "T": "Tango", "U": "Uniform", "V": "Victor", "W": "Whiskey",
    "X": "X-ray", "Y": "Yankee", "Z": "Zulu",
}
_TELEPHONY = {
    "AC": "Air Canada", "ACA": "Air Canada", "TRUCK": "Truck", "FIRE": "Fire",
    "OPS": "Ops", "CAR": "Car", "TUG": "Tug", "SWEEPER": "Sweeper", "PERSON": "Person",
}


def _digits(s: str) -> str:
    return " ".join(_DIGITS[int(ch)] if ch.isdigit() else _PHONETIC.get(ch, ch) for ch in s)


def spoken_callsign(callsign: str) -> str:
    """AC8646 -> "Air Canada eight six four six", TRUCK1 -> "Truck one"."""
    m = re.match(r"^([A-Z]+?)(\d.*)?$", callsign or "")
    if not m:
        return callsign
    prefix, rest = m.group(1), m.group(2) or ""
    name = _TELEPHONY.get(prefix) or " ".join(_PHONETIC.get(c, c) for c in prefix)
    return f"{name} {_digits(rest)}".strip()


def spoken_runway(runway: str | None) -> str:
    return _digits(runway) if runway else "the runway"


def spoken_at(at: str | None) -> str:
    if not at:
        return ""
    return " ".join(_PHONETIC.get(c, c) for c in at.upper() if c.isalnum())


# ----------------------------------------------------------------- triggers


def trigger_kind(decision: dict[str, Any]) -> str | None:
    """"resume" (executed safety stop), "say_again" (executed safety warn), or None."""
    if decision.get("agent") != "safety" or decision.get("outcome") != "executed":
        return None
    action = decision.get("intent_action")
    if action == "stop":
        return "resume"
    if action == "warn":
        text = (decision.get("text") or "").lower()
        rule = decision.get("rule") or ""
        words = re.split(r"[^a-z]+", rule.lower())  # "uncleared_entry" is not "unclear"
        if rule == READBACK_MISMATCH or "say again" in text or UNCLEAR_FLAG in words:
            return "say_again"
    return None


# ---------------------------------------------------------------- templates


def template(facts: dict[str, Any]) -> str:
    """The deterministic call for these facts; always passes ``guard``."""
    cs = spoken_callsign(facts["callsign"])
    if facts["kind"] == "say_again":
        if facts.get("rule") == READBACK_MISMATCH:
            return f"{cs}, negative, say again your readback"
        return f"{cs}, say again"
    other = facts.get("other")
    rwy = spoken_runway(facts.get("runway"))
    at = spoken_at(facts.get("at"))
    at_part = f" at {at}" if at else ""
    other_part = f"the landing {spoken_callsign(other)}" if other else "the traffic"
    instruction = facts.get("instruction")
    if instruction == "land":
        return f"{cs}, continue, expect landing clearance once {other_part} has vacated"
    if instruction == "takeoff":
        return f"{cs}, hold position, expect departure once {other_part} has vacated"
    if facts.get("other_holds_conflict"):
        return (f"{cs}, hold short of runway {rwy}{at_part}, expect to cross after "
                f"{other_part} has vacated")
    return f"{cs}, cross runway {rwy}{at_part} after {other_part} has vacated"


def detail_for(facts: dict[str, Any]) -> str:
    cs, other = facts["callsign"], facts.get("other")
    if facts["kind"] == "say_again":
        why = "the readback did not match" if facts.get("rule") == READBACK_MISMATCH \
            else "the last call was unclear"
        return f"Ask {cs} to say again: {why}."
    rwy = facts.get("runway") or "the runway"
    against = f" against {other}" if other else ""
    return f"{cs} was stopped on runway {rwy}{against}; resume only after the traffic has vacated."


# -------------------------------------------------------------------- guard

_CLEARANCE = re.compile(r"cleared\s+to\s+land|cleared\s+for\s+take-?\s?off|cleared\s+to\s+cross"
                        r"|\bcross\s+runway\b", re.I)
_CONDITIONAL = re.compile(r"\b(after|once|when)\b.*\b(passed|passes|vacated|vacates|clear of)\b",
                          re.I)


def guard(text: str | None, facts: dict[str, Any]) -> str | None:
    """None if the draft is acceptable, else the reason it is rejected."""
    if not text or not text.strip():
        return "empty"
    t = text.strip()
    if len(t) > 220 or "\n" in t:
        return "not a single call"
    cs = facts["callsign"]
    if not (t.lower().startswith(spoken_callsign(cs).lower()) or t.upper().startswith(cs)):
        return "not addressed by callsign"
    if _CLEARANCE.search(t):
        if not _CONDITIONAL.search(t):
            return "unconditional clearance"
        if facts.get("other_holds_conflict"):
            return "conflicting traffic still holds a land / takeoff reservation"
    return None


# ------------------------------------------------------------------ drafting

SYSTEM_PROMPT = (
    "You draft one radio call for an airport tower controller to approve. Use standard ICAO/FAA "
    "phraseology. Exactly one call, one line, addressed first by the spoken callsign given. "
    "Never issue a new landing, takeoff or crossing clearance unless it is explicitly "
    "conditional on the conflicting traffic having passed or vacated. No explanations, no "
    "quotes: output only the words to transmit."
)

Drafter = Callable[[dict[str, Any]], str | None]


def llm_drafter() -> Drafter | None:
    """Drafter backed by holdshort.llm (task "advisor"); None when the provider is "none"."""
    from holdshort import llm

    if llm.provider() == "none":
        return None
    timeout_s = float(os.environ.get("LLM_ADVISOR_TIMEOUT_S", "").strip() or 20)

    def draft(facts: dict[str, Any]) -> str | None:
        user = (f"Situation: {facts.get('summary')}\nAddress: {spoken_callsign(facts['callsign'])}"
                f"\nGoal: {'ask them to say again' if facts['kind'] == 'say_again' else 'resume the stopped movement safely'}"  # noqa: E501
                f"\nTemplate you may improve: {template(facts)}")
        text = llm.text("advisor", SYSTEM_PROMPT, user, timeout_s)
        return text.splitlines()[0].strip().strip('"') if text else None

    return draft


# -------------------------------------------------------------------- graph


class AdvisorState(TypedDict, total=False):
    decision: dict[str, Any]     # JSON-safe copy of the triggering decision
    facts: dict[str, Any]
    template: str
    draft: str | None
    source: str                  # "llm" | "template"
    final: str
    rejected: str | None
    intent_id: str | None
    done: bool


def _json_decision(d: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(d["_id"]), "t": d["t"].isoformat() if d.get("t") else None,
        "tower_id": d.get("tower_id") or TOWER_ID, "intent_id": str(d.get("intent_id") or ""),
        "intent_action": d.get("intent_action"), "rule": d.get("rule"),
        "lane_id": d.get("lane_id"), "stream_id": d.get("stream_id"),
        "text": d.get("text"), "target": list(d.get("target") or []),
        "kind": trigger_kind(d),
    }


def _memory_brief(db, callsigns: list[str], lane_id: str | None) -> str | None:
    try:
        from holdshort.memory.query import brief  # type: ignore[import-not-found]
    except Exception:
        return None
    try:
        return str(brief(db, callsigns=callsigns, lane_ids=[lane_id] if lane_id else []))
    except Exception:
        log.debug("memory brief failed; reading context directly", exc_info=True)
        return None


class Advisor:
    """The gather -> draft -> check -> emit graph for one database."""

    def __init__(self, db: Database, drafter: Drafter | None = None, checkpointer=None):
        from langgraph.checkpoint.mongodb import MongoDBSaver
        from langgraph.graph import END, START, StateGraph

        self.db = db
        self.sdb = scoped_db(db, AGENT)
        self.drafter = drafter
        self.checkpointer = checkpointer or MongoDBSaver(db.client, db_name=db.name)
        g = StateGraph(AdvisorState)
        g.add_node("gather", self.gather)
        g.add_node("draft", self.draft)
        g.add_node("check", self.check)
        g.add_node("emit", self.emit)
        g.add_edge(START, "gather")
        g.add_edge("gather", "draft")
        g.add_edge("draft", "check")
        g.add_edge("check", "emit")
        g.add_edge("emit", END)
        self.graph = g.compile(checkpointer=self.checkpointer)

    # -- nodes

    def gather(self, s: AdvisorState) -> AdvisorState:
        d = s["decision"]
        sdb = self.sdb
        intent = None
        if d.get("intent_id"):
            intent = sdb.intents_safety.find_one({"_id": ObjectId(d["intent_id"])})
        intent = intent or {}
        target = list(d.get("target") or intent.get("target") or [])
        callsign = target[0] if target else "UNKNOWN"
        other = target[1] if len(target) > 1 else None
        if other is None and intent.get("conflicts_with"):
            m = re.match(r"([A-Z0-9]+)", intent["conflicts_with"])
            other = m.group(1) if m else None
        lane_id = d.get("lane_id") or intent.get("lane_id")
        runway = intent.get("runway")
        if not runway and lane_id:
            lane = sdb.lanes.find_one({"_id": lane_id}) or {}
            runway = next((x for x in lane.get("designators") or []
                           if re.fullmatch(r"[0-3][0-9]", x)), None)

        # the addressee's latest clearance and any unclear flag, from working memory
        ctx = list(sdb.context.find(
            {"callsigns": callsign, "superseded_by": None},
            sort=[("t", DESCENDING)], limit=10))
        clr = next((c for c in ctx if c.get("kind") == "clearance" and c.get("instruction")), None)
        obj = sdb.objects.find_one({"_id": callsign}) or sdb.objects.find_one(
            {"callsign": callsign}) or {}
        instruction = ((obj.get("clearance") or {}).get("instruction")
                       or (clr or {}).get("instruction"))
        at = None
        if clr:
            m = re.search(r"\bat\s+([A-Z])\b", clr.get("summary") or "")
            at = m.group(1) if m else None
        unclear = any(UNCLEAR_FLAG in (c.get("flag") or "") for c in ctx)

        # does the conflicting traffic still hold land / takeoff on a lane in conflict?
        holds = False
        if other:
            holds = sdb.lane_status.find_one({
                "state": "conflict",
                "reservations": {"$elemMatch": {"callsign": other,
                                                "instruction": {"$in": ["land", "takeoff"]}}},
            }) is not None

        summary = _memory_brief(sdb, [c for c in (callsign, other) if c], lane_id) or "; ".join(
            c.get("summary", "") for c in ctx[:5])
        rule = d.get("rule") or intent.get("reason")
        if s["decision"]["kind"] == "say_again" and unclear and rule != READBACK_MISMATCH:
            rule = "unclear"
        facts = {
            "kind": d["kind"], "rule": rule, "callsign": callsign, "other": other,
            "lane_id": lane_id, "runway": runway, "at": at, "instruction": instruction,
            "other_holds_conflict": holds, "summary": summary or (d.get("text") or ""),
        }
        return {"facts": facts, "template": template(facts)}

    def draft(self, s: AdvisorState) -> AdvisorState:
        if self.drafter is None:
            return {"draft": None}
        try:
            return {"draft": self.drafter(s["facts"])}
        except Exception as e:
            log.warning("draft failed (%s); using the template", redact(e))
            return {"draft": None}

    def check(self, s: AdvisorState) -> AdvisorState:
        draft = s.get("draft")
        if draft:
            reason = guard(draft, s["facts"])
            if reason is None:
                return {"final": draft.strip(), "source": "llm", "rejected": None}
            log.warning("guard rejected draft %r: %s", draft, reason)
            return {"final": s["template"], "source": "template", "rejected": reason}
        return {"final": s["template"], "source": "template", "rejected": None}

    def emit(self, s: AdvisorState) -> AdvisorState:
        d, facts = s["decision"], s["facts"]
        perm = self.sdb.permissions.find_one({"_id": AGENT}) or {}
        proactive = perm.get("mode") == "proactive"
        reason = "resume_after_stop" if facts["kind"] == "resume" else "say_again"
        intent = Intent(
            t=now(), tower_id=d["tower_id"], agent=AGENT,
            action="propose_instruction" if proactive else "warn", severity="info",
            target=[facts["callsign"]] if facts["callsign"] != "UNKNOWN" else [],
            lane_id=facts.get("lane_id"), runway=facts.get("runway"),
            stream_id=d.get("stream_id"), reason=reason, conflicts_with=facts.get("other"),
            proposal_text=s["final"] if proactive else None, detail=detail_for(facts),
        )
        doc = to_mongo(intent)
        doc["_id"] = ObjectId(d["id"])  # one proposal per triggering decision, ever
        try:
            self.sdb.intents_procedures.insert_one(doc)
            log.info("intent %s: %s %r", doc["_id"], intent.action, intent.proposal_text)
        except DuplicateKeyError:
            log.info("intent %s already emitted", doc["_id"])
        return {"intent_id": d["id"], "done": True}

    # -- run

    def run(self, decision: dict[str, Any]) -> AdvisorState | None:
        """Run (or resume) the thread for this decision. None if it already finished."""
        config = {"configurable": {"thread_id": str(decision["_id"])}}
        snap = self.graph.get_state(config)
        if snap.values.get("done"):
            return None
        if snap.next:  # interrupted mid-run: resume from the checkpoint
            return self.graph.invoke(None, config)
        return self.graph.invoke({"decision": _json_decision(decision)}, config)


class ProceduresAgent(StreamAgent):
    name = "procedures_agent"
    collection = "decisions"

    def __init__(self, db: Database, max_await_ms: int = 300, drafter: Drafter | None = None,
                 checkpointer=None):
        super().__init__(db, max_await_ms)
        self.advisor = Advisor(db, drafter if drafter is not None else llm_drafter(),
                               checkpointer)

    def pipeline(self) -> list[dict]:
        return [{"$match": {
            "operationType": "insert", "ns.coll": "decisions",
            "fullDocument.agent": "safety", "fullDocument.outcome": "executed",
            "fullDocument.intent_action": {"$in": ["stop", "warn"]},
        }}]

    def handle(self, decision: dict[str, Any]) -> AdvisorState | None:
        if trigger_kind(decision) is None:
            return None
        return self.advisor.run(decision)


# ------------------------------------------------------------------------ rogue


def _record(db: Database, attempted: str, err: PyMongoError | None) -> dict[str, Any]:
    if err is None:  # should never happen: the validator let it through
        code, msg = None, "NOT BLOCKED: the write was accepted"
    else:
        code = getattr(err, "code", None)
        details = getattr(err, "details", None) or {}
        msg = redact(details.get("errmsg") or str(err))
    event = SecurityEvent(t=now(), agent=AGENT, kind="validator_rejected", attempted=attempted,
                          error_code=code, error=msg)
    doc = to_mongo(event)
    doc["_id"] = db.security_events.insert_one(doc).inserted_id
    log.warning("security event: %s -> %s %s", attempted, code, msg)
    return doc


def rogue(db: Database) -> list[dict[str, Any]]:
    """Three forbidden actions by a misbehaving procedures agent. Returns the security events."""
    t = now()
    events = []

    attempted = 'insert intents_procedures {action: "clear", target: [TRUCK1], runway: 04}'
    try:
        db.intents_procedures.insert_one({
            "t": t, "tower_id": TOWER_ID, "agent": AGENT, "action": "clear",
            "severity": "info", "target": ["TRUCK1"], "runway": "04",
            "reason": "rogue: clear TRUCK1 to cross", "conflicts_with": None,
            "proposal_text": "TRUCK1 cleared to cross runway 04",
        })
        events.append(_record(db, attempted, None))
    except WriteError as e:
        events.append(_record(db, attempted, e))

    attempted = ("insert decisions {intent_action: propose_instruction, outcome: executed, "
                 "approved_by: none}")
    try:
        db.decisions.insert_one({
            "t": t, "tower_id": TOWER_ID, "intent_id": ObjectId(), "agent": AGENT,
            "intent_action": "propose_instruction", "severity": "info", "outcome": "executed",
            "rule": "rogue_self_approved", "controller_id": "ctl_local", "mode": "proactive",
            "authorized_by": {"permission_id": AGENT, "rule_set_version": "normal_v1"},
            "target": ["TRUCK1"], "approved_by": None, "revokes": None, "notified": ["TRUCK1"],
        })
        events.append(_record(db, attempted, None))
    except WriteError as e:
        events.append(_record(db, attempted, e))

    # reading the permanent log is not allowed: ScopedDb records read_denied itself
    try:
        scoped_db(db, AGENT).transmissions.find_one({})
        events.append(_record(db, "transmissions.find_one", None))
    except AccessDenied as e:
        ev = db.security_events.find_one({"agent": AGENT, "kind": "read_denied",
                                          "attempted": e.attempted}, sort=[("_id", DESCENDING)])
        if ev:
            events.append(ev)
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
            print(f"{ev['_id']} {ev['kind']} code={ev.get('error_code')} {ev['attempted']}")
        return 0
    finally:
        client.close()


if __name__ == "__main__":
    sys.exit(main())
