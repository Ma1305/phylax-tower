"""Agents + scenario runner: pure logic, plus DB tests against "<MONGODB_DB>_test"."""

from datetime import UTC, datetime, timedelta

import pytest

from holdshort.agents.procedures_agent import ProceduresAgent, proposal_text, rogue
from holdshort.agents.safety_agent import SafetyAgent, load_state
from holdshort.agents.weather_agent import WeatherAgent, low_vis_intent
from holdshort.contracts import Metar, to_mongo
from holdshort.gate import decide, set_permission
from holdshort.scenario.runner import addressee, load_script, reset, script_transmission

START = datetime(2026, 3, 22, 23, 36, 30, tzinfo=UTC)


def _script_txs():
    script = load_script()
    return [
        script_transmission(e, script["start"] + timedelta(seconds=e["offset_s"]))
        for e in script["events"]
        if e["type"] == "transmission"
    ]


# ------------------------------------------------------------------------- pure


def test_addressee():
    assert addressee("TOWER", "Truck one and company, cross runway four at Delta.") == "TRUCK1"
    assert addressee("TOWER", "Air Canada eight six four six, ... cleared to land.") == "AC8646"
    assert addressee("TOWER", "Stop, stop, stop!") == "UNKNOWN"
    assert addressee("TRUCK1", "Tower, Truck one ...") == "TOWER"


def test_script_transmissions_match_expect():
    txs = {tx.transcript: tx for tx in _script_txs()}
    cross = next(tx for tx in txs.values() if tx.kind == "clearance" and tx.instruction == "cross")
    assert (cross.from_, cross.to, cross.runway, cross.at) == ("TOWER", "TRUCK1", "04", "D")
    unclear = next(tx for tx in txs.values() if tx.confidence < 0.8)
    assert unclear.verified is False and unclear.kind == "other"


def test_low_vis_intent():
    m = Metar(t=START, visibility_sm=0.5, wind="03012KT")
    intent = low_vis_intent(m, "normal_v1")
    assert intent.action == "set_rule_set" and intent.proposal_text == "low_vis_v1"
    assert low_vis_intent(m, "low_vis_v1") is None
    assert low_vis_intent(Metar(t=START, visibility_sm=3, wind="x"), "normal_v1") is None


def test_proposal_text():
    assert proposal_text("TRUCK1", "04", "D", "AC8646") == (
        "TRUCK1 cross runway 04 at D after AC8646 passes"
    )


# --------------------------------------------------------------------------- db


@pytest.fixture
def db(test_db):
    for a in ("safety", "procedures", "weather", "system"):
        set_permission(test_db, a, True, "test_setup")
    reset(test_db)
    yield test_db
    test_db.rules.update_one({"version": "normal_v1"}, {"$set": {"active": True}})
    test_db.rules.update_one({"version": "low_vis_v1"}, {"$set": {"active": False}})
    reset(test_db)


def test_safety_and_procedures_on_script(db):
    safety, procedures = SafetyAgent(db), ProceduresAgent(db)
    stops = []
    for tx in _script_txs():
        doc = to_mongo(tx)
        db.transmissions.insert_one(doc)
        stops += safety.handle(doc)
    assert len(stops) == 1
    stop = stops[0]
    assert stop.target == ["TRUCK1", "AC8646"] and stop.conflicts_with == "AC8646 cleared to land"
    fs = db.flight_state.find_one({"callsign": "AC8646"})
    assert fs["current_clearance"] == "land" and fs["expires_at"] > datetime.now(UTC)
    assert {c.callsign for c in load_state(db).active("04")} == {"AC8646", "TRUCK1"}

    intent = db.intents_safety.find_one({"t": stop.t}, sort=[("_id", -1)])
    decision = decide(db, "safety", intent)
    assert decision["outcome"] == "executed"
    proposal = procedures.handle(decision)
    assert proposal.proposal_text == "TRUCK1 cross runway 04 at D after AC8646 passes"
    pi = db.intents_procedures.find_one({"proposal_text": proposal.proposal_text},
                                        sort=[("_id", -1)])
    assert decide(db, "procedures", pi)["outcome"] == "pending_approval"


def test_weather_set_rule_set_executes_and_activates(db):
    doc = to_mongo(Metar(t=START, visibility_sm=0.25, wind="03008KT"))
    db.metar.insert_one(doc)
    intent = WeatherAgent(db).handle(doc)
    assert intent is not None
    stored = db.intents_weather.find_one({"t": START, "action": "set_rule_set"},
                                         sort=[("_id", -1)])
    d = decide(db, "weather", stored)
    assert (d["outcome"], d["rule"]) == ("executed", "tier_autonomous")
    assert d["authorized_by"]["rule_set_version"] == "normal_v1"
    active = [r["version"] for r in db.rules.find({"active": True})]
    assert active == ["low_vis_v1"]


def test_unknown_rule_set_rejected(db):
    doc = to_mongo(low_vis_intent(Metar(t=START, visibility_sm=0.1, wind="x"), None))
    doc["proposal_text"] = "no_such_rules"
    doc["_id"] = db.intents_weather.insert_one(doc).inserted_id
    d = decide(db, "weather", doc)
    assert (d["outcome"], d["rule"]) == ("rejected", "unknown_rule_set")


def test_rogue_blocked_and_recorded(db):
    before = db.security_events.count_documents({})
    events = rogue(db)
    assert [e["error_code"] for e in events] == [121, 121]
    assert db.security_events.count_documents({}) == before + 2
    assert db.intents_procedures.count_documents({"action": "clear"}) == 0
