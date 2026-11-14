"""Safety engine (pure) and safety agent (against the test database)."""

from datetime import UTC, datetime, timedelta

import pytest

from holdshort.safety.engine import (
    RULE_INCURSION,
    RULE_LOW_VIS,
    RULE_NO_CROSSING,
    RULE_NO_LANDING_OCCUPIED,
    RULE_READBACK,
    RULE_UNCLEARED,
    LaneView,
    callsign_in_words,
    intents_for,
    lane_conflicts,
    lane_scope,
    new_conflicts,
    readback_conflicts,
)

T0 = datetime(2026, 11, 14, 18, 41, tzinfo=UTC)
NORMAL = {RULE_NO_CROSSING, RULE_NO_LANDING_OCCUPIED, RULE_INCURSION, RULE_UNCLEARED,
          RULE_READBACK, "no_ai_clearances"}
LOW_VIS = NORMAL | {RULE_LOW_VIS}
RWY = LaneView("RWY_04_22", "runway", ["04", "22"])


def obj(oid, kind="aircraft", callsign=None, status="ground", lane_id=None, clearance=None):
    return {"_id": oid, "tower_id": "LGA", "kind": kind, "callsign": callsign, "status": status,
            "position": {"type": "Point", "coordinates": [-73.87, 40.77]}, "lane_id": lane_id,
            "clearance": clearance, "hold_short": False, "updated_at": T0,
            "expires_at": T0 + timedelta(seconds=120)}


def res(callsign, instruction, object_id=None):
    return {"object_id": object_id or callsign, "callsign": callsign,
            "instruction": instruction, "since": T0, "transmission_id": None}


def ls(occupants=(), reservations=(), state="conflict", lane_id="RWY_04_22"):
    return {"_id": lane_id, "tower_id": "LGA", "controller_id": "ctl_local", "state": state,
            "occupants": list(occupants), "reservations": list(reservations), "reason": None,
            "updated_at": T0}


def laguardia():
    objects = {
        "AC8646": obj("AC8646", callsign="AC8646", status="airborne",
                      clearance={"instruction": "land", "lane_id": "RWY_04_22", "since": T0}),
        "TRUCK1": obj("TRUCK1", "vehicle", "TRUCK1", lane_id="TWY_D",
                      clearance={"instruction": "cross", "lane_id": "RWY_04_22", "at": "D",
                                 "since": T0}),
    }
    return ls(reservations=[res("AC8646", "land"), res("TRUCK1", "cross")]), objects


# ------------------------------------------------------------------- engine rules


def test_callsign_words():
    assert callsign_in_words("TRUCK1") == "Truck one"
    assert callsign_in_words("AC8646") == "Air Canada eight six four six"


def test_laguardia_one_critical_stop_truck_only():
    status, objects = laguardia()
    cs = lane_conflicts(status, objects, RWY, NORMAL)
    crit = [c for c in cs if c.severity == "critical"]
    assert len(cs) == 1 and len(crit) == 1
    c = crit[0]
    assert c.rule == RULE_NO_CROSSING and c.targets == ["AC8646", "TRUCK1"]
    intents = intents_for(c, "proactive", tower_id="LGA", t=T0, stream_id="LGA_GND")
    assert [i.action for i in intents] == ["warn", "stop"]
    warn, stop = intents
    assert warn.severity == "critical" and warn.reason == RULE_NO_CROSSING
    assert set(warn.object_ids) == {"AC8646", "TRUCK1"} and warn.lane_id == "RWY_04_22"
    assert stop.target == ["TRUCK1"] and stop.object_ids == ["TRUCK1"]
    assert stop.proposal_text == "Truck one, STOP. Hold short runway zero four."
    assert stop.stream_id == "LGA_GND" and "AC8646" in (stop.conflicts_with or "")


def test_laguardia_truck_on_runway_still_one_conflict():
    status, objects = laguardia()
    status["occupants"] = ["TRUCK1"]
    cs = lane_conflicts(status, objects, RWY, NORMAL)
    assert [c.rule for c in cs] == [RULE_NO_CROSSING]


def test_reactive_mode_warn_only():
    status, objects = laguardia()
    (c,) = lane_conflicts(status, objects, RWY, NORMAL)
    intents = intents_for(c, "reactive", tower_id="LGA", t=T0)
    assert [i.action for i in intents] == ["warn"]


def test_person_without_callsign_is_incursion():
    objects = {"OBJ-17": obj("OBJ-17", "person"),
               "AC8646": obj("AC8646", callsign="AC8646", status="airborne")}
    status = ls(occupants=["OBJ-17"], reservations=[res("AC8646", "land")])
    cs = lane_conflicts(status, objects, RWY, NORMAL)
    assert [c.rule for c in cs] == [RULE_INCURSION]
    c = cs[0]
    assert c.severity == "critical" and c.object_ids == ["AC8646", "OBJ-17"]
    warn, stop = intents_for(c, "proactive", tower_id="LGA", t=T0)
    assert stop.target == [] and stop.object_ids == ["OBJ-17"]
    assert stop.proposal_text == "Person on runway zero four, STOP. Vacate the runway immediately."


def test_landing_on_occupied_runway_stops_the_occupant_not_the_lander():
    objects = {"AC100": obj("AC100", callsign="AC100", lane_id="RWY_04_22",
                            clearance={"instruction": "line_up_wait", "lane_id": "RWY_04_22",
                                       "since": T0}),
               "AC8646": obj("AC8646", callsign="AC8646", status="airborne")}
    status = ls(occupants=["AC100"],
                reservations=[res("AC100", "line_up_wait"), res("AC8646", "land")])
    cs = lane_conflicts(status, objects, RWY, NORMAL)
    assert [c.rule for c in cs] == [RULE_NO_LANDING_OCCUPIED]
    stop = intents_for(cs[0], "proactive", tower_id="LGA", t=T0)[1]
    assert stop.target == ["AC100"]


def test_uncleared_entry_caution_no_stop():
    objects = {"TRUCK2": obj("TRUCK2", "vehicle", "TRUCK2")}
    cs = lane_conflicts(ls(occupants=["TRUCK2"], state="occupied"), objects, RWY, NORMAL)
    assert [(c.rule, c.severity) for c in cs] == [(RULE_UNCLEARED, "caution")]
    assert [i.action for i in intents_for(cs[0], "proactive", tower_id="LGA", t=T0)] == ["warn"]


def test_cleared_occupant_alone_is_quiet():
    objects = {"TRUCK1": obj("TRUCK1", "vehicle", "TRUCK1")}
    status = ls(occupants=["TRUCK1"], reservations=[res("TRUCK1", "cross")], state="occupied")
    assert lane_conflicts(status, objects, RWY, NORMAL) == []


def test_low_vis_only_in_low_vis_rule_set():
    objects = {"AC1": obj("AC1", callsign="AC1"), "TRUCK1": obj("TRUCK1", "vehicle", "TRUCK1")}
    status = ls(occupants=["AC1"], reservations=[res("AC1", "cross"), res("TRUCK1", "cross")])
    assert lane_conflicts(status, objects, RWY, NORMAL) == []
    cs = lane_conflicts(status, objects, RWY, LOW_VIS)
    assert [(c.rule, c.severity) for c in cs] == [(RULE_LOW_VIS, "warning")]


def test_readback_mismatch():
    item = {"_id": "x", "kind": "clearance", "summary": "AC8646 read back runway 13",
            "callsigns": ["AC8646", "TOWER"], "lane_ids": ["RWY_04_22"], "readback": "mismatch",
            "source": {"coll": "transmissions", "id": "t1"}}
    (c,) = readback_conflicts(item, NORMAL)
    assert c.severity == "warning" and c.targets == ["AC8646"]
    assert readback_conflicts({**item, "readback": "matched"}, NORMAL) == []


def test_dedupe_across_repeated_updates_and_rearm():
    status, objects = laguardia()
    active: set[str] = set()
    emitted = 0
    for _ in range(3):
        fresh, active = new_conflicts(lane_conflicts(status, objects, RWY, NORMAL), active,
                                      lane_scope("RWY_04_22"))
        emitted += len(fresh)
    assert emitted == 1
    fresh, active = new_conflicts([], active, lane_scope("RWY_04_22"))  # cleared
    assert active == set()
    fresh, active = new_conflicts(lane_conflicts(status, objects, RWY, NORMAL), active,
                                  lane_scope("RWY_04_22"))
    assert len(fresh) == 1


# ------------------------------------------------------------------- agent (Atlas)


@pytest.fixture
def agent_db(test_db):
    for coll in ("objects", "lane_status", "context", "intents_safety", "agent_state"):
        test_db[coll].delete_many({} if coll != "lane_status" else {"_id": "RWY_04_22"})
    test_db.permissions.update_one({"_id": "safety"}, {"$set": {"mode": "proactive"}})
    from holdshort.access import scoped_db
    scoped_db(test_db, "safety").reload()
    yield test_db
    test_db.permissions.update_one({"_id": "safety"}, {"$set": {"mode": "proactive"}})


def _load_laguardia(db):
    status, objects = laguardia()
    db.objects.insert_many(list(objects.values()))
    db.context.insert_one({"tower_id": "LGA", "kind": "clearance", "summary": "GND->TRUCK1 cross",
                           "callsigns": ["TRUCK1"], "lane_ids": ["RWY_04_22"],
                           "stream_id": "LGA_GND", "source": {"coll": "transmissions", "id": 1},
                           "t": T0, "expires_at": datetime.now(UTC) + timedelta(minutes=15)})
    db.lane_status.replace_one({"_id": "RWY_04_22"}, status, upsert=True)
    return status


def test_agent_laguardia_no_duplicates_and_restart(agent_db):
    from holdshort.agents.safety_agent import SafetyAgent

    status = _load_laguardia(agent_db)
    a = SafetyAgent(agent_db)
    for _ in range(3):
        a.handle(status, coll="lane_status")
    docs = list(agent_db.intents_safety.find({}).sort("action", 1))
    assert [(d["action"], d["target"]) for d in docs] == [
        ("stop", ["TRUCK1"]), ("warn", ["AC8646", "TRUCK1"])]
    stop = docs[0]
    assert stop["stream_id"] == "LGA_GND"
    assert stop["proposal_text"] == "Truck one, STOP. Hold short runway zero four."
    # a restart reads the keys back from agent_state and does not repeat them
    SafetyAgent(agent_db).handle(status, coll="lane_status")
    assert agent_db.intents_safety.count_documents({}) == 2


def test_agent_reactive_emits_no_stop(agent_db):
    from holdshort.access import scoped_db
    from holdshort.agents.safety_agent import SafetyAgent

    status = _load_laguardia(agent_db)
    agent_db.permissions.update_one({"_id": "safety"}, {"$set": {"mode": "reactive"}})
    scoped_db(agent_db, "safety").reload()
    SafetyAgent(agent_db).handle(status, coll="lane_status")
    assert [d["action"] for d in agent_db.intents_safety.find({})] == ["warn"]


def test_scoped_handle_denies_transmissions(agent_db):
    from holdshort.access import AccessDenied
    from holdshort.agents.safety_agent import SafetyAgent

    a = SafetyAgent(agent_db)
    with pytest.raises(AccessDenied):
        a.scoped.transmissions.find_one({})
