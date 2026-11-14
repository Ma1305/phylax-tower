"""Action gate against Atlas ("<MONGODB_DB>_test"), with the gate in a background thread."""

import threading
import time
from datetime import UTC, datetime

import pytest

from holdshort.contracts import Intent, to_mongo
from holdshort.gate import Gate, approve, revoke, set_permission

T = datetime(2026, 3, 22, 23, 37, 5, tzinfo=UTC)
AGENTS = ("safety", "procedures", "weather", "listener", "system")


def _start(db) -> tuple[Gate, threading.Thread]:
    gate = Gate(db, max_await_ms=200)
    th = threading.Thread(target=gate.run, daemon=True)
    th.start()
    assert gate.ready.wait(20), "change stream did not open"
    return gate, th


def _stop(gate: Gate, th: threading.Thread) -> None:
    gate.stop()
    th.join(10)
    assert not th.is_alive()


@pytest.fixture
def db(test_db):
    test_db.gate_state.delete_many({})
    for a in AGENTS:
        set_permission(test_db, a, True, "test_setup")
    yield test_db
    for a in AGENTS:
        set_permission(test_db, a, True, "test_teardown")


@pytest.fixture
def gate(db):
    g, th = _start(db)
    yield g
    _stop(g, th)


def _insert(db, agent: str, action: str = "stop", **kw) -> object:
    fields = dict(
        t=T,
        agent=agent,
        action=action,
        target=["TRUCK1"],
        runway="04",
        reason="AC8646 cleared to land 04",
        conflicts_with="AC8646",
    )
    fields.update(kw)
    return db[f"intents_{agent}"].insert_one(to_mongo(Intent(**fields))).inserted_id


def _wait_decision(db, intent_id, timeout: float = 5.0, **query) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        d = db.decisions.find_one({"intent_id": intent_id, **query})
        if d:
            return d
        time.sleep(0.05)
    raise AssertionError(f"no decision for intent {intent_id} within {timeout}s")


def test_stop_executed_within_5s(db, gate):
    start = time.monotonic()
    iid = _insert(db, "safety")
    d = _wait_decision(db, iid, timeout=5)
    assert time.monotonic() - start < 5
    assert d["outcome"] == "executed"
    assert d["agent"] == "safety" and d["intent_action"] == "stop"
    assert d["notified"] == ["TRUCK1"]
    perm = db.permissions.find_one({"agent": "safety"})
    assert d["authorized_by"] == {"permission_id": perm["_id"], "rule_set_version": "normal_v1"}


def test_disabled_agent_rejected(db, gate):
    set_permission(db, "procedures", False, "controller_A")
    iid = _insert(
        db,
        "procedures",
        "propose_instruction",
        proposal_text="TRUCK1 cross runway 04 at D after AC8646 passes",
    )
    d = _wait_decision(db, iid)
    assert (d["outcome"], d["rule"]) == ("rejected", "agent_disabled")
    assert d["notified"] == []
    assert db.permissions.find_one({"agent": "procedures"})["by"] == "controller_A"


def test_master_switch_off_rejects_everything(db, gate):
    set_permission(db, "system", False, "controller_A")
    ids = [
        _insert(db, "safety"),
        _insert(db, "procedures", "propose_instruction"),
        _insert(db, "weather", "close_runway"),
    ]
    for iid in ids:
        d = _wait_decision(db, iid)
        assert (d["outcome"], d["rule"]) == ("rejected", "agent_disabled")


def test_tier_never_rejected(db, gate):
    db.permissions.update_one({"agent": "weather"}, {"$set": {"tier": "never"}})
    try:
        d = _wait_decision(db, _insert(db, "weather", "close_runway"))
        assert (d["outcome"], d["rule"]) == ("rejected", "tier_never")
    finally:
        db.permissions.update_one({"agent": "weather"}, {"$set": {"tier": "autonomous"}})


def test_identity_comes_from_collection(db, gate):
    # bypass the validator to plant a lying "agent" field in intents_safety
    doc = to_mongo(Intent(t=T, agent="safety", action="stop", target=["TRUCK1"], reason="x"))
    doc["agent"] = "procedures"
    iid = db.intents_safety.insert_one(doc, bypass_document_validation=True).inserted_id
    d = _wait_decision(db, iid)
    assert d["agent"] == "safety"


def test_propose_approve_then_revoke(db, gate):
    iid = _insert(
        db,
        "procedures",
        "propose_instruction",
        proposal_text="TRUCK1 cross runway 04 at D after AC8646 passes",
    )
    pending = _wait_decision(db, iid)
    assert pending["outcome"] == "pending_approval"
    assert pending.get("approved_by") is None

    executed = approve(db, iid, "controller_A")
    assert executed["_id"] != pending["_id"]
    assert executed["outcome"] == "executed"
    assert executed["approved_by"] == "controller_A"
    assert executed["notified"] == ["TRUCK1"]
    assert executed["t"] >= pending["t"]

    revoked = revoke(db, executed["_id"], "controller_A")
    assert revoked["outcome"] == "revoked"
    assert revoked["revokes"] == executed["_id"]
    assert revoked["intent_id"] == iid

    # insert-only: the earlier decisions are unchanged
    outcomes = [d["outcome"] for d in db.decisions.find({"intent_id": iid}).sort("_id", 1)]
    assert outcomes == ["pending_approval", "executed", "revoked"]


def test_restart_processes_exactly_once(db):
    gate, th = _start(db)
    first = _insert(db, "safety")
    _wait_decision(db, first)
    _stop(gate, th)

    missed = _insert(db, "safety", target=["TRUCK2"])  # inserted while the gate is down
    time.sleep(0.5)
    assert db.decisions.count_documents({"intent_id": missed}) == 0

    gate, th = _start(db)
    try:
        d = _wait_decision(db, missed)
        assert d["outcome"] == "executed"
        time.sleep(1.0)  # give a duplicate a chance to appear
    finally:
        _stop(gate, th)
    assert db.decisions.count_documents({"intent_id": missed}) == 1
    assert db.decisions.count_documents({"intent_id": first}) == 1

    # a third run that resumes again must not re-decide anything
    gate, th = _start(db)
    time.sleep(1.0)
    _stop(gate, th)
    assert db.decisions.count_documents({"intent_id": missed}) == 1
    assert db.decisions.count_documents({"intent_id": first}) == 1
