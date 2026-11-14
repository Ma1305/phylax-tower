"""Gate v2 and /control/* API tests (own test db, the gate runs in a thread)."""

from __future__ import annotations

import threading
import time
from datetime import UTC, datetime

import pytest
from bson import ObjectId

from holdshort import gate


def _wait(fn, timeout=15.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        v = fn()
        if v:
            return v
        time.sleep(0.1)
    raise AssertionError("timed out")


@pytest.fixture
def db(test_db):
    test_db.decisions.delete_many({})
    test_db.security_events.delete_many({})
    for coll in ("intents_safety", "intents_procedures", "intents_weather"):
        test_db[coll].delete_many({})
    for agent, mode, tier in [("safety", "proactive", "needs_approval"),
                              ("procedures", "proactive", "needs_approval"),
                              ("weather", "proactive", "autonomous"),
                              ("system", "proactive", "autonomous")]:
        test_db.permissions.update_one(
            {"_id": agent}, {"$set": {"enabled": True, "mode": mode, "tier": tier}}
        )
    test_db.rules.update_many({}, {"$set": {"active": False}})
    test_db.rules.update_one({"version": "normal_v1"}, {"$set": {"active": True}})
    return test_db


def _gate_thread(db):
    g = gate.Gate(db, max_await_ms=200)
    th = threading.Thread(target=g.run, daemon=True)
    th.start()
    assert g.ready.wait(15)
    return g, th


@pytest.fixture
def running(db):
    db.gate_state.delete_many({})
    g, th = _gate_thread(db)
    yield db
    g.stop()
    th.join(10)


def _intent(db, agent="safety", action="stop", lane_id="RWY_04_22", **kw):
    doc = {
        "t": datetime.now(UTC), "tower_id": "LGA", "agent": agent, "action": action,
        "severity": "critical", "target": ["AC8646"], "object_ids": ["AC8646"],
        "lane_id": lane_id, "runway": "04", "stream_id": None, "reason": "runway_incursion",
        "conflicts_with": "TRUCK1", "proposal_text": "AC8646 go around",
        "detail": "TRUCK1 is on runway 04 while AC8646 is cleared to land.",
    }
    doc.update(kw)
    return db[f"intents_{agent}"].insert_one(doc).inserted_id


def _decision(db, iid):
    return _wait(lambda: db.decisions.find_one({"intent_id": iid}))


def test_warn_is_advisory(running):
    d = _decision(running, _intent(running, action="warn"))
    assert (d["outcome"], d["rule"]) == ("executed", "advisory")
    assert d["text"].startswith("TRUCK1")
    assert d["controller_id"] == "ctl_local" and d["stream_id"] == "LGA_TWR"


def test_reactive_stop_rejected(running):
    gate.set_permission(running, "safety", "ctl_sup", mode="reactive")
    d = _decision(running, _intent(running))
    assert (d["outcome"], d["rule"], d["mode"]) == ("rejected", "mode_reactive", "reactive")


def test_pending_routing_and_approval(running):
    iid = _intent(running)
    d = _decision(running, iid)
    assert (d["outcome"], d["rule"]) == ("pending_approval", "needs_approval")
    assert d["controller_id"] == "ctl_local" and d["lane_id"] == "RWY_04_22"
    assert d["text"] == "AC8646 go around" and d["mode"] == "proactive"
    assert d["authorized_by"]["permission_id"] == "safety"

    with pytest.raises(gate.GateError) as e:
        gate.approve(running, d["_id"], "ctl_ground")
    assert str(e.value) == "not_lane_owner" and e.value.status == 403
    ev = running.security_events.find_one({"kind": "not_lane_owner"})
    assert ev["controller_id"] == "ctl_ground"

    ok = gate.approve(running, d["_id"], "ctl_local")
    assert (ok["outcome"], ok["rule"], ok["approved_by"]) == (
        "executed", "controller_approved", "ctl_local")
    assert ok["_id"] != d["_id"]
    assert running.decisions.find_one({"_id": d["_id"]})["outcome"] == "pending_approval"
    with pytest.raises(gate.GateError) as e:
        gate.approve(running, d["_id"], "ctl_local")
    assert e.value.status == 409

    rv = gate.revoke(running, ok["_id"], "ctl_local")
    assert (rv["outcome"], rv["rule"], rv["revokes"]) == (
        "revoked", "controller_revoked", ok["_id"])


def test_supervisor_may_approve(running):
    d = _decision(running, _intent(running))
    ok = gate.approve(running, d["_id"], "ctl_sup")
    assert ok["approved_by"] == "ctl_sup"


def test_autonomous_stop_executes(running):
    gate.set_permission(running, "safety", "ctl_sup", tier="autonomous")
    d = _decision(running, _intent(running))
    assert (d["outcome"], d["rule"]) == ("executed", "tier_autonomous")


def test_reject(running):
    d = _decision(running, _intent(running, action="propose_instruction"))
    assert d["outcome"] == "pending_approval"
    r = gate.reject(running, d["_id"], "ctl_local", "not now")
    assert (r["outcome"], r["rule"], r["revokes"], r["text"]) == (
        "revoked", "controller_rejected", d["_id"], "not now")
    with pytest.raises(gate.GateError):
        gate.approve(running, d["_id"], "ctl_local")


def test_master_switch_off(running):
    gate.set_permission(running, "system", "ctl_sup", enabled=False)
    for action in ("warn", "stop", "propose_instruction"):
        d = _decision(running, _intent(running, action=action))
        assert (d["outcome"], d["rule"]) == ("rejected", "agent_disabled")


def test_permission_change_logged(db):
    before = db.state_events.count_documents({"kind": "permission", "ref": "procedures"})
    doc = gate.set_permission(db, "procedures", "ctl_sup", enabled=False, tier="never")
    assert doc["enabled"] is False and doc["tier"] == "never"
    ev = db.state_events.find_one({"kind": "permission", "ref": "procedures"}, sort=[("_id", -1)])
    n = db.state_events.count_documents({"kind": "permission", "ref": "procedures"})
    assert n == before + 1
    assert ev["by"] == "ctl_sup" and ev["doc"]["tier"] == "never"
    with pytest.raises(gate.GateError):
        gate.set_permission(db, "procedures", "ctl_sup", mode="sideways")


def test_close_lane_and_rule_set_side_effects(running):
    gate.set_permission(running, "weather", "ctl_sup", tier="autonomous")
    d = _decision(running, _intent(running, agent="weather", action="set_rule_set",
                                   proposal_text="low_vis_v1", lane_id=None))
    assert d["outcome"] == "executed" and d["controller_id"] == "ctl_sup"
    _wait(lambda: running.state_events.find_one({"kind": "rule_set", "ref": "low_vis_v1"}))
    assert [r["version"] for r in running.rules.find({"active": True})] == ["low_vis_v1"]

    d = _decision(running, _intent(running, action="close_lane", lane_id="TWY_B"))
    assert d["outcome"] == "pending_approval" and d["controller_id"] == "ctl_ground"
    gate.approve(running, d["_id"], "ctl_ground")
    assert running.lane_status.find_one({"_id": "TWY_B"})["state"] == "closed"
    d = _decision(running, _intent(running, action="reopen_lane", lane_id="TWY_B"))
    gate.approve(running, d["_id"], "ctl_ground")
    assert running.lane_status.find_one({"_id": "TWY_B"})["state"] == "clear"
    assert running.state_events.count_documents({"kind": "lane_status", "ref": "TWY_B",
                                                 "by": "ctl_ground"}) == 2


def test_identity_from_collection(db):
    # the validator already refuses a mismatched agent; the gate still ignores the field
    iid = _intent(db, agent="procedures", action="warn")
    doc = {**db.intents_procedures.find_one({"_id": iid}), "agent": "weather"}
    d = gate.Gate(db).handle({"ns": {"coll": "intents_procedures"}, "fullDocument": doc})
    assert d["agent"] == "procedures" and d["authorized_by"]["permission_id"] == "procedures"


def test_restart_exactly_once(db):
    db.gate_state.delete_many({})
    g, th = _gate_thread(db)
    first = _intent(db, action="warn")
    _decision(db, first)
    g.stop()
    th.join(10)
    second = _intent(db, action="warn")  # arrives while the gate is down
    g, th = _gate_thread(db)
    try:
        _decision(db, second)
        time.sleep(1.0)
        assert db.decisions.count_documents({"intent_id": first}) == 1
        assert db.decisions.count_documents({"intent_id": second}) == 1
        # deciding an already-decided intent again is a no-op
        gate.decide(db, "safety", db.intents_safety.find_one({"_id": first}))
        assert db.decisions.count_documents({"intent_id": first}) == 1
    finally:
        g.stop()
        th.join(10)


# ------------------------------------------------------------------ API


@pytest.fixture
def api(db):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from holdshort.api.routers.control import router

    app = FastAPI()
    app.state.db = db
    app.include_router(router)
    with TestClient(app) as tc:
        yield tc


def test_api_flow(api, db):
    r = api.get("/control/controllers")
    assert r.status_code == 200
    assert {c["_id"] for c in r.json()["controllers"]} >= {"ctl_local", "ctl_ground", "ctl_sup"}

    iid = _intent(db)
    d = gate.decide(db, "safety", db.intents_safety.find_one({"_id": iid}))
    did = str(d["_id"])

    mine = api.get("/control/pending", params={"controller_id": "ctl_local"}).json()["pending"]
    item = next(p for p in mine if p["_id"] == did)
    assert item["intent"]["reason"] == "runway_incursion" and item["t"].endswith("Z")
    assert not any(p["_id"] == did for p in api.get(
        "/control/pending", params={"controller_id": "ctl_ground"}).json()["pending"])
    assert any(p["_id"] == did for p in api.get(
        "/control/pending", params={"controller_id": "ctl_sup"}).json()["pending"])
    assert api.get("/control/pending", params={"controller_id": "nobody"}).status_code == 404

    body = {"decision_id": did, "controller_id": "ctl_ground"}
    r = api.post("/control/approve", json=body)
    assert r.status_code == 403 and r.json()["detail"]["error"] == "not_lane_owner"
    assert api.post("/control/approve", json={**body, "decision_id": "zz"}).status_code == 400
    assert api.post("/control/approve",
                    json={**body, "decision_id": str(ObjectId())}).status_code == 404
    r = api.post("/control/approve", json={**body, "controller_id": "ctl_local"})
    assert r.status_code == 200 and r.json()["approved_by"] == "ctl_local"
    assert api.post("/control/reject",
                    json={**body, "controller_id": "ctl_local"}).status_code == 409
    assert not any(p["_id"] == did for p in api.get(
        "/control/pending", params={"controller_id": "ctl_local"}).json()["pending"])
    r = api.post("/control/revoke", json={"decision_id": r.json()["_id"],
                                          "controller_id": "ctl_sup"})
    assert r.status_code == 200 and r.json()["rule"] == "controller_revoked"

    r = api.post("/control/permissions/safety", json={"by": "ctl_sup", "mode": "reactive"})
    assert r.status_code == 200 and r.json()["mode"] == "reactive"
    assert api.post("/control/permissions/safety",
                    json={"by": "ctl_sup", "tier": "bogus"}).status_code == 400
    assert api.post("/control/permissions/nobody", json={"by": "x",
                                                        "enabled": True}).status_code == 404
    assert api.post("/control/permissions/safety", json={"by": "x"}).status_code == 400


def test_api_reject(api, db):
    iid = _intent(db, action="propose_instruction")
    d = gate.decide(db, "safety", db.intents_safety.find_one({"_id": iid}))
    r = api.post("/control/reject", json={"decision_id": str(d["_id"]),
                                          "controller_id": "ctl_local", "reason": "no"})
    assert r.status_code == 200 and r.json()["rule"] == "controller_rejected"


def test_api_decision_log(api, db):
    gate.set_permission(db, "safety", "ctl_sup", tier="autonomous")
    t0 = datetime.now(UTC)
    stop = gate.decide(db, "safety", db.intents_safety.find_one({"_id": _intent(db)}))
    prop = gate.decide(db, "procedures", db.intents_procedures.find_one(
        {"_id": _intent(db, agent="procedures", action="propose_instruction")}))
    assert (stop["outcome"], stop["rule"], stop["controller_id"]) == (
        "executed", "tier_autonomous", "ctl_local")
    gate.approve(db, prop["_id"], "ctl_local")
    gate.revoke(db, stop["_id"], "ctl_local")
    r = api.get("/control/decisions", params={"tower_id": "LGA", "since": t0.isoformat()})
    assert r.status_code == 200
    docs = r.json()["decisions"]
    assert [d["rule"] for d in docs] == ["controller_revoked", "controller_approved",
                                         "needs_approval", "tier_autonomous"]
    assert all(d["intent"]["_id"] == d["intent_id"] for d in docs)
    assert docs[-1]["intent"]["reason"] == "runway_incursion"
    r = api.get("/control/decisions", params={"controller_id": "ctl_ground",
                                              "since": t0.isoformat()})
    assert r.json()["decisions"] == []
    assert len(api.get("/control/decisions", params={"limit": 1}).json()["decisions"]) == 1
    assert api.get("/control/decisions", params={"limit": 0}).status_code == 422
