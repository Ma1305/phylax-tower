"""State service: clearances, hold_short, lane_status and its state_events."""

from datetime import UTC, datetime, timedelta

from bson import ObjectId

from holdshort.state.core import StateEngine, build_lane_status, transmission_effect

T0 = datetime(2026, 11, 14, 18, 0, 0, tzinfo=UTC)
LANES = [
    {"_id": "RWY_04_22", "tower_id": "LGA", "kind": "runway"},
    {"_id": "TWY_D", "tower_id": "LGA", "kind": "taxiway"},
]
OWNERS = {"RWY_04_22": "ctl_local", "TWY_D": "ctl_ground"}
DESIG = {"04": "RWY_04_22", "22": "RWY_04_22"}


def at(s: float) -> datetime:
    return T0 + timedelta(seconds=s)


def tx(t, to, instruction, kind="clearance", frm="TOWER", direction="in", runway="04",
       at_=None, transcript=""):
    return {"_id": ObjectId(), "tower_id": "LGA", "t": t, "direction": direction,
            "from": frm, "to": to, "kind": kind, "instruction": instruction,
            "runway": runway, "at": at_, "lane_id": None, "transcript": transcript}


def obj(oid, t, lane_id=None, kind="aircraft", status="ground", alt_ft=0):
    return {"_id": oid, "tower_id": "LGA", "kind": kind, "callsign": oid, "status": status,
            "alt_ft": alt_ft, "lane_id": lane_id, "clearance": None, "hold_short": False,
            "updated_at": t}


def engine() -> StateEngine:
    return StateEngine(LANES, OWNERS, lambda x: x.get("lane_id") or DESIG.get(x.get("runway")))


def test_transmission_effects():
    assert transmission_effect(tx(T0, "AC8646", "land")) == ("AC8646", "set", False)
    assert transmission_effect(tx(T0, "AC8646", "hold_short")) == ("AC8646", "end", True)
    assert transmission_effect(tx(T0, "AC8646", "go_around")) == ("AC8646", "end", None)
    assert transmission_effect(tx(T0, "TRUCK1", "none", kind="stop", frm="HOLDSHORT",
                                  direction="out")) == ("TRUCK1", "end_cross", True)
    rep = tx(T0, "TOWER", "none", kind="report", frm="AC8646",
             transcript="AC8646 clear of runway 04 at B")
    assert transmission_effect(rep) == ("AC8646", "end", None)
    assert transmission_effect(tx(T0, "AC8646", "taxi", kind="request")) is None


def test_landing_and_crossing_scenario():
    """reserved -> conflict -> (occupant) -> clear; one lane write per change."""
    e = engine()
    e.status = {lid: build_lane_status(lane, OWNERS[lid], {}, {}, T0)
                for lid, lane in ((x["_id"], x) for x in LANES)}
    states: list[str] = []

    def flush(t):
        w = e.flush(t)
        if "RWY_04_22" in w.lanes:
            states.append(w.lanes["RWY_04_22"]["state"])
        return w

    e.on_object(obj("AC8646", at(0), status="airborne", alt_ft=1500), at(0))
    e.on_object(obj("TRUCK1", at(0), lane_id="TWY_D", kind="vehicle"), at(0))
    flush(at(0))
    assert states == []

    e.on_transmission(tx(at(1), "AC8646", "land"), at(1))
    w = flush(at(1))
    assert w.objects["AC8646"]["clearance"]["instruction"] == "land"
    assert w.objects["AC8646"]["clearance"]["lane_id"] == "RWY_04_22"
    assert w.objects["AC8646"]["hold_short"] is False
    assert states == ["reserved"]

    e.on_transmission(tx(at(2), "TRUCK1", "cross", frm="GROUND", at_="D"), at(2))
    w = flush(at(2))
    assert states == ["reserved", "conflict"]
    doc = w.lanes["RWY_04_22"]
    assert {r["callsign"] for r in doc["reservations"]} == {"AC8646", "TRUCK1"}
    assert doc["controller_id"] == "ctl_local" and doc["reason"]

    # the truck's position inside the runway makes it an occupant (still conflict)
    for s in range(3, 8):   # a burst of 1 Hz updates -> at most one write
        e.on_object(obj("TRUCK1", at(s), lane_id="RWY_04_22", kind="vehicle"), at(s))
    w = flush(at(8))
    assert w.lanes["RWY_04_22"]["occupants"] == ["TRUCK1"]
    assert states == ["reserved", "conflict", "conflict"]
    assert flush(at(8)).lanes == {}   # nothing changed, nothing written

    # truck crosses and leaves; 30 s later its clearance ends
    e.on_object(obj("TRUCK1", at(10), lane_id="TWY_D", kind="vehicle"), at(10))
    flush(at(10))
    flush(at(41))
    assert e.objects["TRUCK1"]["clearance"] is None

    # aircraft lands (occupant), vacates, reports clear -> clearance ends -> clear
    e.on_object(obj("AC8646", at(50), lane_id="RWY_04_22"), at(50))
    flush(at(50))
    e.on_object(obj("AC8646", at(60), lane_id="TWY_D"), at(60))
    flush(at(60))
    assert states[-1] == "reserved"
    w = flush(at(91))
    assert w.objects["AC8646"]["clearance"] is None
    assert states[-1] == "clear"
    # one write per real change: occupants / reservations changed between the conflicts
    assert states == ["reserved", "conflict", "conflict", "conflict", "reserved", "occupied",
                      "reserved", "clear"]


def test_outbound_stop_sets_hold_short():
    e = engine()
    e.on_object(obj("TRUCK1", at(0), lane_id="TWY_D", kind="vehicle"), at(0))
    e.on_transmission(tx(at(1), "TRUCK1", "cross", frm="GROUND", at_="D"), at(1))
    e.flush(at(1))
    e.on_transmission(tx(at(2), "TRUCK1", "none", kind="stop", frm="HOLDSHORT",
                         direction="out", runway=None), at(2))
    w = e.flush(at(2))
    assert w.objects["TRUCK1"] == {"clearance": None, "hold_short": True}
    assert w.lanes["RWY_04_22"]["state"] == "clear"


def test_reservation_by_callsign_without_object():
    e = engine()
    e.on_transmission(tx(at(0), "AC1234", "takeoff", runway="22"), at(0))
    w = e.flush(at(0))
    r = w.lanes["RWY_04_22"]["reservations"]
    assert w.lanes["RWY_04_22"]["state"] == "reserved"
    assert r[0]["object_id"] is None and r[0]["callsign"] == "AC1234"
    # the object shows up: the clearance moves onto it
    e.on_object(obj("AC1234", at(10)), at(10))
    w = e.flush(at(10))
    assert w.objects["AC1234"]["clearance"]["instruction"] == "takeoff"
    assert w.lanes["RWY_04_22"]["reservations"][0]["object_id"] == "AC1234"
    # a callsign that never appears is dropped after 5 minutes
    e.on_transmission(tx(at(20), "AC9999", "land", runway="13", ), at(20))
    e.on_transmission(tx(at(20), "AC7777", "land"), at(20))
    e.flush(at(20))
    assert "AC7777" in e.pending
    e.flush(at(321))
    assert "AC7777" not in e.pending


def test_closed_lane_left_alone():
    e = engine()
    closed = build_lane_status(LANES[0], "ctl_local", {}, {}, T0) | {"state": "closed",
                                                                     "reason": "fod"}
    e.load_status(closed)
    e.on_transmission(tx(at(1), "AC8646", "land"), at(1))
    assert "RWY_04_22" not in e.flush(at(1)).lanes
    e.load_status(closed | {"state": "clear", "reason": None})   # reopened by the gate
    assert e.flush(at(2)).lanes["RWY_04_22"]["state"] == "reserved"


def test_worker_writes_lane_status_and_state_events(test_db):
    from holdshort.state.worker import StateWorker

    db = test_db
    db.objects.delete_many({})
    db.transmissions.delete_many({})   # other test modules share this database
    w = StateWorker(db)
    w.recompute()   # settles whatever earlier test modules left behind
    before = db.state_events.count_documents({"kind": "lane_status", "ref": "RWY_04_22"})
    now = datetime.now(UTC)
    o = obj("AC8646", now, status="airborne", alt_ft=900) | {
        "position": {"type": "Point", "coordinates": [-73.95, 40.72]},
        "expires_at": now + timedelta(seconds=120)}
    db.objects.insert_one(o)
    w.handle(o, coll="objects")
    t = tx(now, "AC8646", "land") | {"stream_id": "LGA_TWR", "controller_id": "ctl_local"}
    w.handle(t, coll="transmissions")
    w.flush()
    st = db.lane_status.find_one({"_id": "RWY_04_22"})
    assert st["state"] == "reserved"
    assert db.objects.find_one({"_id": "AC8646"})["clearance"]["instruction"] == "land"
    events = list(db.state_events.find({"kind": "lane_status", "ref": "RWY_04_22"})
                  .sort("t", 1))
    assert len(events) == before + 1 and events[-1]["doc"]["state"] == "reserved"
    w.flush()   # no change -> no new event
    w.handle(st, coll="lane_status")   # echo of our own write
    w.flush()
    assert db.state_events.count_documents(
        {"kind": "lane_status", "ref": "RWY_04_22"}) == before + 1
    db.objects.delete_one({"_id": "AC8646"})
    w.handle({"_id": "AC8646"}, coll="objects")
    w.flush()
    assert db.lane_status.find_one({"_id": "RWY_04_22"})["state"] == "clear"
