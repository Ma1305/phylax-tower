"""Safety agent: transmissions -> rule engine -> stop intents in intents_safety.

    uv run python -m holdshort.agents.safety_agent [--db NAME]

For every inserted transmission (in order) it

1. loads the AirfieldState from flight_state (so a scenario reset, which clears
   flight_state, also resets the agent, and a restarted agent picks up where it was);
2. runs ``engine.evaluate`` against the active rule set (state BEFORE the call);
3. inserts each resulting stop intent into intents_safety (never acts itself);
4. folds the call into the state (``engine.apply_transmission``) and persists the
   changed callsigns to flight_state with a TTL ``expires_at``.

Callsigns with ``hold_short`` set (held by a stop) are not active on a runway.

Inside a scenario run the four steps are a LangGraph graph (evaluate -> emit -> persist)
checkpointed to Atlas with thread_id = run id (see ``holdshort.agents._graph``): a killed
safety agent resumes from its checkpoint and only processes calls it has not seen. The
decision itself stays the deterministic rule engine; there is no LLM in it.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta
from types import MappingProxyType
from typing import Any, TypedDict

from bson import ObjectId
from pymongo.database import Database

from holdshort.agents._graph import GraphAgent
from holdshort.agents._stream import main, now
from holdshort.contracts import FlightState, Intent, RuleSet, Transmission, to_mongo
from holdshort.safety.engine import (
    RUNWAY_INSTRUCTIONS,
    AirfieldState,
    Clearance,
    apply_transmission,
    evaluate,
)

FLIGHT_STATE_TTL = timedelta(hours=1)
"""flight_state documents expire this long (wall clock) after their last update."""

NOT_TRAFFIC = frozenset({"TOWER", "UNKNOWN"})


def load_state(db: Database) -> AirfieldState:
    """Rebuild the active runway clearances from flight_state."""
    runways: dict[str, list[Clearance]] = {}
    query = {
        "current_clearance": {"$in": sorted(RUNWAY_INSTRUCTIONS)},
        "runway": {"$ne": None},
        "hold_short": {"$ne": True},
    }
    for doc in db.flight_state.find(query).sort("updated_at", 1):
        runways.setdefault(doc["runway"], []).append(
            Clearance(doc["callsign"], doc["current_clearance"], doc["runway"], doc["updated_at"])
        )
    return AirfieldState(MappingProxyType({r: tuple(cs) for r, cs in runways.items()}))


def active_rule_set(db: Database) -> RuleSet | None:
    doc = db.rules.find_one({"active": True}, sort=[("version", 1)])
    return RuleSet.model_validate(doc) if doc else None


def _clearance(state: AirfieldState, callsign: str) -> Clearance | None:
    cs = state.clearances_of(callsign)
    return cs[-1] if cs else None


def state_updates(
    before: AirfieldState, after: AirfieldState, tx: Transmission
) -> dict[str, dict[str, Any]]:
    """flight_state fields to $set per callsign whose state changed because of ``tx``."""
    callsigns = {
        c.callsign
        for s in (before, after)
        for cs in s.runways.values()
        for c in cs
    }
    callsigns.add(tx.to)
    out: dict[str, dict[str, Any]] = {}
    for cs in sorted(callsigns - NOT_TRAFFIC):
        old, new = _clearance(before, cs), _clearance(after, cs)
        if cs != tx.to and old == new:
            continue
        if new is not None:
            fields = {"current_clearance": new.instruction, "runway": new.runway,
                      "hold_short": False}
        elif cs == tx.to and tx.kind in ("clearance", "stop"):
            fields = {
                "current_clearance": None if tx.instruction == "none" else tx.instruction,
                "runway": tx.runway,
                "hold_short": tx.kind == "stop" or tx.instruction == "hold_short",
            }
        elif cs == tx.to:
            continue  # requests / readbacks addressed to traffic change nothing
        else:
            fields = {"current_clearance": None, "runway": None, "hold_short": False}
        out[cs] = fields
    return out


def persist(db: Database, updates: dict[str, dict[str, Any]], t: datetime) -> None:
    expires = now() + FLIGHT_STATE_TTL
    for cs, fields in updates.items():
        doc = to_mongo(FlightState(callsign=cs, updated_at=t, expires_at=expires, **fields))
        doc.pop("position")  # owned by the scenario runner
        db.flight_state.update_one({"callsign": cs}, {"$set": doc}, upsert=True)


class SafetyGraphState(TypedDict, total=False):
    """Checkpointed per run (JSON-safe: ids are strings, models are JSON dicts)."""

    tx_id: str
    tx: dict[str, Any]
    intents: list[dict[str, Any]]
    updates: dict[str, dict[str, Any]]
    intent_ids: list[str]
    last_id: str
    processed: int
    stops: int


class SafetyAgent(GraphAgent):
    name = "safety"
    collection = "transmissions"
    state_schema = SafetyGraphState

    # -- the deterministic steps (shared by handle() and the graph)

    def evaluate_tx(self, tx: Transmission) -> tuple[list[Intent], dict[str, dict[str, Any]]]:
        state = load_state(self.db)
        rules = active_rule_set(self.db)
        intents: list[Intent] = []
        if rules is None:
            self.log.warning("no active rule set; not evaluating %s", tx.transcript)
        else:
            intents = evaluate(tx, state, rules)
        return intents, state_updates(state, apply_transmission(state, tx), tx)

    def emit(self, intents: list[Intent]) -> list[ObjectId]:
        ids = []
        for intent in intents:
            iid = self.db.intents_safety.insert_one(to_mongo(intent)).inserted_id
            ids.append(iid)
            self.log.info("intent %s: stop %s (%s; %s)", iid, intent.target, intent.reason,
                          intent.conflicts_with)
        return ids

    def handle(self, doc: dict[str, Any]) -> list[Intent]:
        tx = Transmission.model_validate(doc)
        intents, updates = self.evaluate_tx(tx)
        self.emit(intents)
        persist(self.db, updates, tx.t)
        return intents

    # -- graph

    def graph_input(self, doc: dict[str, Any]) -> SafetyGraphState:
        tx = Transmission.model_validate(doc)
        return {"tx_id": str(doc["_id"]), "tx": tx.model_dump(mode="json", by_alias=True)}

    def nodes(self):
        return [("evaluate", self._n_evaluate), ("emit", self._n_emit),
                ("persist", self._n_persist)]

    def _n_evaluate(self, s: SafetyGraphState) -> SafetyGraphState:
        intents, updates = self.evaluate_tx(Transmission.model_validate(s["tx"]))
        return {"intents": [i.model_dump(mode="json") for i in intents], "updates": updates}

    def _n_emit(self, s: SafetyGraphState) -> SafetyGraphState:
        ids = self.emit([Intent.model_validate(i) for i in s.get("intents", [])])
        return {"intent_ids": [str(i) for i in ids],
                "stops": s.get("stops", 0) + len(ids)}

    def _n_persist(self, s: SafetyGraphState) -> SafetyGraphState:
        tx = Transmission.model_validate(s["tx"])
        persist(self.db, s.get("updates", {}), tx.t)
        return {"last_id": s["tx_id"], "processed": s.get("processed", 0) + 1}


if __name__ == "__main__":
    sys.exit(main(SafetyAgent))
