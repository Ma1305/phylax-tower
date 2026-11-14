"""Foundation, against the test database: setup, validators, world helpers, ScopedDb,
StreamAgent resume."""

import threading
import time
from datetime import UTC, datetime

import pytest
from bson import ObjectId
from pymongo.errors import WriteError

from holdshort import world
from holdshort.access import AccessDenied, ScopedDb, scoped_db
from holdshort.agents._stream import StreamAgent
from holdshort.events import log_state_event

NOW = datetime.now(UTC)


def intent(agent="safety", action="stop", **kw):
    return {"t": datetime.now(UTC), "tower_id": "LGA", "agent": agent, "action": action,
            "severity": "critical", "target": ["TRUCK1"], "lane_id": "RWY_04_22",
            "reason": "runway_incursion", **kw}


def test_setup_twice(test_db):
    import setup_atlas

    lines: list[str] = []
    test_db.permissions.update_one({"_id": "safety"}, {"$set": {"enabled": False}})
    setup_atlas.setup(test_db, log=lines.append)
    assert test_db.permissions.find_one({"_id": "safety"})["enabled"] is False  # kept
    test_db.permissions.update_one({"_id": "safety"}, {"$set": {"enabled": True}})
    assert any("0 new" in x for x in lines)
    assert test_db.lanes.count_documents({"tower_id": "LGA"}) == 6
    assert test_db.lane_status.count_documents({"state": "clear"}) == 6
    assert test_db.rules.find_one({"active": True})["version"] == "normal_v1"
    low = test_db.rules.find_one({"version": "low_vis_v1"})
    assert "low_vis_one_movement" in [r["id"] for r in low["rules"]]
    opts = test_db.positions.options()
    assert opts["timeseries"]["timeField"] == "t" and opts["expireAfterSeconds"] == 86400
    lane = test_db.lanes.find_one({"_id": "TWY_D"})
    ring = lane["area"]["coordinates"][0]
    assert ring[0] == ring[-1] and len(ring) >= 4


def test_reset_live(test_db):
    import setup_atlas

    test_db.lane_status.update_one({"_id": "TWY_D"}, {"$set": {"state": "occupied"}})
    test_db.objects.insert_one({"_id": "X1", "expires_at": NOW})
    setup_atlas.setup(test_db, log=lambda *_: None, reset_live=True)
    assert test_db.lane_status.find_one({"_id": "TWY_D"})["state"] == "clear"
    assert test_db.objects.count_documents({}) == 0


def test_valid_intent_accepted(test_db):
    test_db.intents_safety.insert_one(intent())


def test_clear_intent_rejected(test_db):
    with pytest.raises(WriteError) as e:
        test_db.intents_procedures.insert_one(intent("procedures", "clear"))
    assert e.value.code == 121


def test_intent_claiming_other_agent_rejected(test_db):
    with pytest.raises(WriteError):
        test_db.intents_procedures.insert_one(intent("safety"))


def decision(**kw):
    return {"t": datetime.now(UTC), "tower_id": "LGA", "intent_id": ObjectId(),
            "agent": "procedures", "intent_action": "propose_instruction",
            "severity": "warning", "outcome": "executed", "rule": "controller_approved",
            "controller_id": "ctl_local", "mode": "proactive",
            "authorized_by": {"permission_id": "procedures", "rule_set_version": "normal_v1"},
            **kw}


def test_executed_proposal_needs_approved_by(test_db):
    with pytest.raises(WriteError):
        test_db.decisions.insert_one(decision())
    with pytest.raises(WriteError):
        test_db.decisions.insert_one(decision(approved_by=""))
    test_db.decisions.insert_one(decision(approved_by="ctl_local"))
    test_db.decisions.insert_one(decision(outcome="pending_approval", approved_by=None))


def test_lane_at(test_db):
    world.clear_cache()
    pts = world.world(test_db, "LGA")["points"]
    assert world.lane_at(test_db, *pts["D_CROSSING"]) == "RWY_04_22"
    hp = test_db.hold_points.find_one({"_id": "HP_D_SE"})["position"]["coordinates"]
    assert world.lane_at(test_db, *hp) == "TWY_D"
    assert world.lane_at(test_db, -73.8700, 40.7900) is None  # Flushing Bay


def test_world_helpers(test_db):
    w = world.world(test_db, "LGA")
    assert w["tower"]["_id"] == "LGA" and len(w["streams"]) == 3
    assert world.lane_for_designator(test_db, "LGA", "04") == "RWY_04_22"
    assert world.lane_for_designator(test_db, "LGA", "22") == "RWY_04_22"
    assert world.lane_for_designator(test_db, "LGA", "D") == "TWY_D"
    assert world.owner_of(test_db, "RWY_04_22") == "ctl_local"
    assert world.owner_of(test_db, "TWY_D") == "ctl_ground"
    assert world.owner_of(test_db, None) == "ctl_sup"
    assert world.owner_of(test_db, "NOPE") == "ctl_sup"
    assert world.streams_of(test_db, "ctl_ground") == ["LGA_GND", "LGA_OPS"]
    assert world.station_of(test_db, "LGA_TWR") == "TOWER"


def test_scoped_db(test_db):
    s = scoped_db(test_db, "safety")
    assert isinstance(s, ScopedDb)
    s.context.find_one({})
    s["intents_safety"].insert_one(intent())
    s.intents_safety.count_documents({})  # write implies read
    with pytest.raises(AccessDenied):
        s.decisions.insert_one({"x": 1})
    with pytest.raises(AccessDenied):
        s.lane_status.update_one({"_id": "TWY_D"}, {"$set": {"state": "closed"}})
    with pytest.raises(AccessDenied):
        s.transmissions.find_one({})
    ev = list(test_db.security_events.find({"agent": "safety"}).sort("t", 1))
    kinds = [(e["kind"], e["attempted"]) for e in ev[-3:]]
    assert kinds == [("write_denied", "decisions.insert_one"),
                     ("write_denied", "lane_status.update_one"),
                     ("read_denied", "transmissions.find_one")]


def test_state_event(test_db):
    _id = log_state_event(test_db, "permission", "safety", {"enabled": True}, by="ctl_sup")
    assert test_db.state_events.find_one({"_id": _id})["by"] == "ctl_sup"


class Recorder(StreamAgent):
    name = "test_recorder"
    watch_collections = ["weather", "sim_runs"]

    def __init__(self, db):
        super().__init__(db, max_await_ms=100)
        self.seen: list[tuple[str, str]] = []

    def handle(self, doc, coll=None):
        self.seen.append((coll, doc.get("note") or doc.get("raw")))


def _run(agent):
    th = threading.Thread(target=agent.run, daemon=True)
    th.start()
    assert agent.ready.wait(15)
    return th


def _wait(cond, timeout=15):
    end = time.time() + timeout
    while time.time() < end and not cond():
        time.sleep(0.1)
    return cond()


def test_stream_agent_resumes(test_db):
    test_db.agent_state.delete_one({"_id": Recorder.name})
    a = Recorder(test_db)
    th = _run(a)
    test_db.weather.insert_one({"t": NOW, "tower_id": "LGA", "visibility_sm": 5, "raw": "one"})
    test_db.sim_runs.insert_one({"tower_id": "LGA", "scenario": "s", "kind": "normal",
                                 "note": "two", "started_at": NOW})
    assert _wait(lambda: len(a.seen) == 2)
    a.stop()
    th.join(10)
    assert a.seen == [("weather", "one"), ("sim_runs", "two")]
    assert test_db.agent_state.find_one({"_id": Recorder.name})["resume_token"]

    # inserted while the agent is down: picked up after the restart
    test_db.weather.insert_one({"t": NOW, "tower_id": "LGA", "visibility_sm": 5,
                                "raw": "three"})
    b = Recorder(test_db)
    th = _run(b)
    assert b.resumed
    assert _wait(lambda: len(b.seen) >= 1)
    b.stop()
    th.join(10)
    assert b.seen == [("weather", "three")]


def test_stream_agent_bad_token_falls_back(test_db):
    test_db.agent_state.update_one({"_id": "test_bad"},
                                   {"$set": {"resume_token": {"_data": "00"}}}, upsert=True)

    class Bad(Recorder):
        name = "test_bad"

    a = Bad(test_db)
    th = _run(a)
    a.stop()
    th.join(10)
    assert not a.resumed


def test_setup_migrates_untouched_stop_tier(test_db):
    """Stops are autonomous by default: setup moves a seeded, never-changed safety permission
    from needs_approval to autonomous (with a state event) and keeps a controller's choice."""
    import setup_atlas

    assert setup_atlas.PERMISSION_SEED["safety"]["tier"] == "autonomous"
    try:
        for by in ("setup", "setup_script"):
            test_db.permissions.update_one({"_id": "safety"},
                                           {"$set": {"tier": "needs_approval", "by": by}})
            before = test_db.state_events.count_documents({"kind": "permission", "ref": "safety"})
            lines: list[str] = []
            setup_atlas.setup(test_db, log=lines.append)
            perm = test_db.permissions.find_one({"_id": "safety"})
            assert (perm["tier"], perm["by"]) == ("autonomous", "setup_script")
            ev = test_db.state_events.find_one({"kind": "permission", "ref": "safety"},
                                               sort=[("t", -1), ("_id", -1)])
            assert test_db.state_events.count_documents(
                {"kind": "permission", "ref": "safety"}) == before + 1
            assert ev["doc"]["tier"] == "autonomous" and ev["by"] == "setup_script"
            assert any("needs_approval -> autonomous" in x for x in lines)
        test_db.permissions.update_one({"_id": "safety"},
                                       {"$set": {"tier": "needs_approval", "by": "ctl_sup"}})
        setup_atlas.setup(test_db, log=lambda *_: None)
        assert test_db.permissions.find_one({"_id": "safety"})["tier"] == "needs_approval"
    finally:
        test_db.permissions.update_one({"_id": "safety"},
                                       {"$set": {"tier": "autonomous", "by": "setup_script"}})


def test_listener_reads_gridfs_audio(test_db):
    import gridfs

    fid = gridfs.GridFSBucket(test_db, bucket_name="audio").upload_from_stream("t", b"abc")
    s = scoped_db(test_db, "listener")
    assert s["audio.files"].find_one({"_id": fid})["length"] == 3
    assert s["audio.chunks"].find_one({"files_id": fid}) is not None
