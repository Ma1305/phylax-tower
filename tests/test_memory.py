"""Working memory: writer, retirement, sweep, search, brief, stats (short lifetimes)."""

from __future__ import annotations

import re
import threading
import time
from datetime import UTC, datetime, timedelta

import pytest
from bson import ObjectId

from holdshort.memory import Memory, Writer
from holdshort.memory.query import BRIEF_TOKEN_LIMIT, brief, search, stats

SHORT = {"clearance": 30, "request": 30, "report": 30, "alert": 30, "note": 30,
         "weather": 30}


@pytest.fixture
def db(test_db):
    test_db.context.delete_many({})
    test_db.objects.delete_many({})
    return test_db


@pytest.fixture
def writer(db):
    return Writer(db, dict(SHORT), grace_s=0.5)


def tx(cs, kind="clearance", instruction="land", lane="RWY_04_22", text=None, t=None,
       frm="TOWER", **kw):
    return {"_id": ObjectId(), "tower_id": "LGA", "stream_id": "LGA_TWR",
            "controller_id": "ctl_local", "t": t or datetime.now(UTC), "direction": "in",
            "from": frm, "to": cs, "kind": kind, "instruction": instruction,
            "runway": "04", "lane_id": lane, "confidence": 0.95, "readback": "not_applicable",
            "verified": True, "flag": None,
            "transcript": text or f"{cs} runway four cleared to land", **kw}


def live(db, **q):
    return list(db.context.find({"expires_at": {"$gt": datetime.now(UTC)}, **q}))


def test_newer_instruction_supersedes_and_sweep_removes(db, writer):
    t0 = datetime.now(UTC) - timedelta(seconds=5)
    old = writer.from_transmission(tx("AC100", t=t0))
    new = writer.from_transmission(tx("AC100", instruction="go_around", t=t0 + timedelta(1 / 86400),
                                      text="AC100 go around"))
    doc = db.context.find_one({"_id": old})
    assert doc["superseded_by"] == new
    assert doc["summary"].split()[1] == "TWR" and "AC100" in doc["callsigns"]
    assert [i["_id"] for i in search(db, "LGA", callsigns=["AC100"])] == [new]
    assert {i["_id"] for i in search(db, "LGA", callsigns=["AC100"],
                                     include_superseded=True)} == {old, new}
    time.sleep(0.7)
    assert writer.sweep() >= 1
    assert db.context.find_one({"_id": old}) is None
    assert db.context.find_one({"_id": new}) is not None


def test_clear_of_runway_report_supersedes_runway_clearance(db, writer):
    c = writer.from_transmission(tx("AC200"))
    r = writer.from_transmission(tx("TOWER", kind="report", instruction="none", frm="AC200",
                                    text="AC200 clear of runway four at delta"))
    assert db.context.find_one({"_id": c})["superseded_by"] == r
    assert db.context.find_one({"_id": r})["kind"] == "report"


def test_kind_mapping_and_decisions(db, writer):
    kinds = {k: db.context.find_one({"_id": writer.from_transmission(
        tx("AC3" + str(i), kind=k, instruction="none"))})["kind"]
        for i, k in enumerate(["request", "readback", "report", "stop", "other"])}
    assert kinds == {"request": "request", "readback": "report", "report": "report",
                     "stop": "alert", "other": "note"}
    d = {"_id": ObjectId(), "t": datetime.now(UTC), "tower_id": "LGA", "intent_action": "stop",
         "severity": "critical", "outcome": "executed", "rule": "runway_incursion",
         "lane_id": "RWY_04_22", "stream_id": "LGA_TWR", "text": "TRUCK1 stop",
         "target": ["TRUCK1"]}
    a = writer.from_decision(d)
    assert db.context.find_one({"_id": a})["kind"] == "alert"
    assert writer.from_decision({**d, "_id": ObjectId(), "outcome": "pending_approval"}) is None


def test_object_removed_retires_items(db):
    """End to end through the change stream: deleting the object expires its items."""
    m = Memory(db, lifetimes=SHORT, grace_s=0.5, sweep_s=0.3)
    m.store_token = False
    th = threading.Thread(target=m.run, daemon=True)
    th.start()
    assert m.ready.wait(20)
    try:
        now = datetime.now(UTC)
        db.objects.insert_one({"_id": "AC400", "tower_id": "LGA", "kind": "aircraft",
                               "callsign": "AC400", "status": "ground",
                               "position": {"type": "Point", "coordinates": [-73.87, 40.77]},
                               "lane_id": "RWY_04_22", "updated_at": now,
                               "expires_at": now + timedelta(seconds=120)})
        db.transmissions.insert_one(tx("AC400"))
        deadline = time.time() + 15
        while time.time() < deadline and not live(db, callsigns="AC400"):
            time.sleep(0.2)
        assert live(db, callsigns="AC400")
        db.objects.delete_one({"_id": "AC400"})
        deadline = time.time() + 15
        while time.time() < deadline and db.context.count_documents({"callsigns": "AC400"}):
            time.sleep(0.2)
        assert db.context.count_documents({"callsigns": "AC400"}) == 0  # expired and swept
    finally:
        m.stop()
        th.join(10)


def test_search_filters(db, writer):
    writer.from_transmission(tx("AC501"))
    writer.from_transmission(tx("AC502", lane="TWY_D", instruction="taxi",
                                text="AC502 taxi via delta"))
    writer.from_transmission(tx("AC503", kind="request", instruction="none", lane=None,
                                text="AC503 request taxi"))
    assert {i["callsigns"][0] for i in search(db, "LGA", lane_ids=["TWY_D"])} == {"AC502"}
    assert {i["callsigns"][0] for i in search(db, "LGA", kinds=["request"])} == {"AC503"}
    assert [i["callsigns"][0] for i in search(db, "LGA", text="TAXI VIA", method="regex")] \
        == ["AC502"]
    hits = search(db, "LGA")
    assert [h["t"] for h in hits] == sorted((h["t"] for h in hits), reverse=True)
    assert search(db, "LGA", limit=2).__len__() == 2
    assert search(db, "JFK") == []
    s = stats(db)
    assert s["live"] == 3 and s["superseded"] == 0 and "transmissions" in s["log"]


def test_brief_stays_small_with_big_log(db, writer):
    lifetimes = {k: 600 for k in SHORT}
    w = Writer(db, lifetimes, grace_s=60)
    t0 = datetime.now(UTC) - timedelta(minutes=10)
    docs = [tx(f"AC{1000 + i}", t=t0 + timedelta(seconds=i),
               lane=["RWY_04_22", "TWY_D", "RWY_13_31"][i % 3],
               kind=["clearance", "request", "report"][i % 3]) for i in range(500)]
    db.transmissions.insert_many(docs)
    for d in docs:
        w.from_transmission(d)
    text = brief(db, "LGA", "RWY_04_22")
    approx_tokens = len(re.findall(r"\w+|[^\w\s]", text))
    assert approx_tokens < BRIEF_TOKEN_LIMIT, approx_tokens
    assert "LANE RWY_04_22" in text and "MEMORY" in text and "TWY_D" in text


def test_weather_supersession(db, writer):
    t0 = datetime.now(UTC)
    a = writer.from_weather({"_id": ObjectId(), "t": t0, "tower_id": "LGA",
                             "visibility_sm": 10, "wind": "040/08"})
    b = writer.from_weather({"_id": ObjectId(), "t": t0 + timedelta(seconds=1),
                             "tower_id": "LGA", "visibility_sm": 0.5, "wind": "040/12"})
    assert db.context.find_one({"_id": a})["superseded_by"] == b
    wx = search(db, "LGA", kinds=["weather"])
    assert [i["_id"] for i in wx] == [b] and "vis 0.5" in wx[0]["summary"]
