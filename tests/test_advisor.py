"""Procedures (advisor) and weather agents against the test database. The LLM is stubbed."""

from datetime import UTC, datetime, timedelta

import pytest
from bson import ObjectId

from holdshort.agents import procedures_agent as pa
from holdshort.agents.weather_agent import WeatherAgent


def _now():
    return datetime.now(UTC)


@pytest.fixture
def db(test_db):
    test_db.intents_procedures.delete_many({})
    test_db.intents_weather.delete_many({})
    test_db.context.delete_many({})
    test_db.permissions.update_one({"_id": "procedures"}, {"$set": {"mode": "proactive"}})
    lane = test_db.lane_status.find_one({"_id": "RWY_04_22"})
    yield test_db
    test_db.permissions.update_one({"_id": "procedures"}, {"$set": {"mode": "proactive"}})
    test_db.lane_status.replace_one({"_id": "RWY_04_22"}, lane)


def _stop_world(db, conflict=True):
    """TRUCK1 cleared to cross 04 at D, AC8646 cleared to land 04, safety stop executed."""
    t = _now()
    db.context.insert_one({
        "tower_id": "LGA", "kind": "clearance", "t": t, "expires_at": t + timedelta(minutes=15),
        "summary": "18:41:07 GND GROUND->TRUCK1 cross runway 04 at D (conf 0.95)",
        "callsigns": ["TRUCK1"], "lane_ids": ["RWY_04_22"], "stream_id": "LGA_GND",
        "instruction": "cross", "readback": "matched", "flag": None,
        "source": {"coll": "transmissions", "id": str(ObjectId())}, "superseded_by": None,
    })
    db.lane_status.update_one({"_id": "RWY_04_22"}, {"$set": {
        "state": "conflict" if conflict else "occupied", "occupants": ["TRUCK1"],
        "reservations": [{"object_id": "AC8646", "callsign": "AC8646", "instruction": "land",
                          "since": t, "transmission_id": None}] if conflict else [],
    }})
    iid = db.intents_safety.insert_one({
        "t": t, "tower_id": "LGA", "agent": "safety", "action": "stop", "severity": "critical",
        "target": ["TRUCK1", "AC8646"], "lane_id": "RWY_04_22", "runway": "04",
        "stream_id": "LGA_GND", "reason": "no_crossing_during_active_landing",
        "conflicts_with": "AC8646 cleared to land",
    }).inserted_id
    return _decision(db, iid, "stop", "no_crossing_during_active_landing",
                     ["TRUCK1", "AC8646"], "TRUCK1, hold position")


def _decision(db, intent_id, action, rule, target, text, stream="LGA_GND"):
    doc = {
        "t": _now(), "tower_id": "LGA", "intent_id": intent_id, "agent": "safety",
        "intent_action": action, "severity": "critical", "outcome": "executed", "rule": rule,
        "lane_id": "RWY_04_22", "controller_id": "ctl_local", "stream_id": stream,
        "text": text, "target": target, "mode": "proactive",
        "authorized_by": {"permission_id": "safety", "rule_set_version": "normal_v1"},
        "approved_by": "ctl_local", "revokes": None, "notified": [],
    }
    doc["_id"] = db.decisions.insert_one(doc).inserted_id
    return doc


def test_stop_yields_one_proposal_and_restart_does_not_duplicate(db):
    d = _stop_world(db, conflict=False)
    agent = pa.ProceduresAgent(db, drafter=lambda f: None)
    agent.handle(d)
    docs = list(db.intents_procedures.find())
    assert len(docs) == 1
    p = docs[0]
    assert p["action"] == "propose_instruction" and p["severity"] == "info"
    assert p["lane_id"] == "RWY_04_22" and p["stream_id"] == "LGA_GND"
    assert p["target"] == ["TRUCK1"] and p["tower_id"] == "LGA" and p["detail"]
    assert p["proposal_text"] == ("Truck one, cross runway zero four at Delta after the "
                                  "landing Air Canada eight six four six has vacated")
    # a restarted agent (fresh process state, same checkpoints) does nothing
    again = pa.ProceduresAgent(db, drafter=lambda f: None)
    assert again.handle(d) is None
    assert db.intents_procedures.count_documents({}) == 1


def test_killed_mid_run_resumes_once(db):
    d = _stop_world(db)

    def crash(_facts):
        raise KeyboardInterrupt  # the process dies inside draft

    with pytest.raises(KeyboardInterrupt):
        pa.ProceduresAgent(db, drafter=crash).handle(d)
    assert db.intents_procedures.count_documents({}) == 0
    pa.ProceduresAgent(db, drafter=lambda f: None).handle(d)
    pa.ProceduresAgent(db, drafter=lambda f: None).handle(d)
    p = db.intents_procedures.find_one()
    assert db.intents_procedures.count_documents({}) == 1
    # AC8646 still holds a land reservation on a conflicting lane: no crossing words at all
    assert "cross runway" not in p["proposal_text"]
    assert p["proposal_text"].startswith("Truck one, hold short of runway zero four")


def test_guard_blocks_unconditional_clearance(db):
    d = _stop_world(db, conflict=False)
    out = pa.ProceduresAgent(db, drafter=lambda f: "Truck one, cross runway zero four").handle(d)
    assert out["rejected"] == "unconditional clearance" and out["source"] == "template"
    p = db.intents_procedures.find_one()
    assert p["proposal_text"].endswith("has vacated")

    facts = {"callsign": "TRUCK1", "other": "AC8646", "other_holds_conflict": True}
    assert pa.guard("Truck one, cleared to cross runway zero four", facts)
    assert pa.guard("Truck one, cross runway zero four after Air Canada has vacated", facts)
    assert pa.guard("Air Canada eight six four six, cleared to land runway zero four",
                    {"callsign": "AC8646"})
    assert pa.guard("Truck one, hold position", facts) is None


def test_llm_draft_used_when_guard_passes(db):
    d = _stop_world(db, conflict=False)
    text = "Truck one, after the landing Air Canada has vacated, cross runway zero four at Delta"
    out = pa.ProceduresAgent(db, drafter=lambda f: text).handle(d)
    assert out["source"] == "llm"
    assert db.intents_procedures.find_one()["proposal_text"] == text


def test_say_again_and_reactive_mode(db):
    iid = db.intents_safety.insert_one({
        "t": _now(), "tower_id": "LGA", "agent": "safety", "action": "warn",
        "severity": "caution", "target": ["AC8646"], "reason": "readback_mismatch",
    }).inserted_id
    d = _decision(db, iid, "warn", "readback_mismatch", ["AC8646"], "AC8646 readback mismatch",
                  stream="LGA_TWR")
    pa.ProceduresAgent(db, drafter=lambda f: None).handle(d)
    p = db.intents_procedures.find_one()
    assert p["action"] == "propose_instruction" and p["stream_id"] == "LGA_TWR"
    assert p["proposal_text"].startswith("Air Canada eight six four six, ")
    assert "say again" in p["proposal_text"]

    db.permissions.update_one({"_id": "procedures"}, {"$set": {"mode": "reactive"}})
    d2 = _decision(db, iid, "warn", "unclear_transmission", ["AC8646"],
                   "AC8646 unclear - say again", stream="LGA_TWR")
    pa.ProceduresAgent(db, drafter=lambda f: None).handle(d2)
    w = db.intents_procedures.find_one({"_id": d2["_id"]})
    assert w["action"] == "warn" and w["proposal_text"] is None and w["detail"]
    assert db.intents_procedures.count_documents({"action": "propose_instruction"}) == 1


def test_untriggered_decisions_ignored(db):
    d = _decision(db, ObjectId(), "warn", "uncleared_entry", ["TRUCK1"], "TRUCK1 on runway")
    assert pa.ProceduresAgent(db, drafter=lambda f: None).handle(d) is None
    assert db.intents_procedures.count_documents({}) == 0


def test_rogue_three_security_events(db):
    before = db.security_events.count_documents({"agent": "procedures"})
    events = pa.rogue(db)
    assert len(events) == 3
    assert [e["kind"] for e in events] == ["validator_rejected", "validator_rejected",
                                           "read_denied"]
    assert events[0]["error_code"] == 121 and events[1]["error_code"] == 121
    assert events[2]["attempted"] == "transmissions.find_one"
    assert db.security_events.count_documents({"agent": "procedures"}) == before + 3
    assert db.intents_procedures.count_documents({"action": "clear"}) == 0


def _set_active(db, version):
    db.rules.update_many({}, {"$set": {"active": False}})
    db.rules.update_one({"version": version}, {"$set": {"active": True}})


def test_weather_both_directions(db):
    agent = WeatherAgent(db)
    wx = {"_id": ObjectId(), "t": _now(), "tower_id": "LGA", "visibility_sm": 0.5,
          "wind": "040/08", "raw": None}
    try:
        _set_active(db, "normal_v1")
        i = agent.handle(wx)
        assert i.proposal_text == "low_vis_v1" and i.severity == "caution"
        assert agent.handle(wx) is None  # not repeated
        assert agent.handle({**wx, "visibility_sm": 2.0}) is None

        _set_active(db, "low_vis_v1")
        assert agent.handle({**wx, "visibility_sm": 0.25}) is None  # already low vis
        assert agent.handle({**wx, "visibility_sm": 2.0}) is None   # hysteresis
        i = agent.handle({**wx, "visibility_sm": 3.0})
        assert i.proposal_text == "normal_v1" and i.action == "set_rule_set"
        docs = list(db.intents_weather.find().sort("t", 1))
        assert [x["proposal_text"] for x in docs] == ["low_vis_v1", "normal_v1"]
    finally:
        _set_active(db, "normal_v1")


def test_guard_blocks_unconditional_clearance_from_llm_layer(db, monkeypatch):
    """The default drafter goes through holdshort.llm; the guard still blocks its output."""
    from holdshort import llm

    monkeypatch.setenv("LLM_PROVIDER", "vertex")
    calls = []
    monkeypatch.setattr(llm, "text", lambda task, system, user, timeout_s: (
        calls.append((task, timeout_s)) or "Truck one, cross runway zero four"))
    d = _stop_world(db, conflict=False)
    out = pa.ProceduresAgent(db).handle(d)
    assert calls == [("advisor", 20.0)]
    assert out["rejected"] == "unconditional clearance" and out["source"] == "template"
