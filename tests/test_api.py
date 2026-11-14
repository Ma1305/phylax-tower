"""API tests: FastAPI TestClient against Atlas ("<MONGODB_DB>_test")."""

import queue
import threading
import time
from datetime import UTC, datetime

import pytest
from bson import ObjectId
from fastapi.testclient import TestClient

from holdshort.api import app as api
from holdshort.api.feed import iso, jsonable, to_event
from holdshort.contracts import Intent, to_mongo
from holdshort.gate import Gate, set_permission

T = datetime(2026, 3, 22, 23, 37, 5, tzinfo=UTC)
AGENTS = ("safety", "procedures", "weather", "listener", "system")

# ------------------------------------------------------------------ pure units


def test_jsonable_objectid_and_datetime():
    oid = ObjectId()
    out = jsonable({"_id": oid, "t": T, "xs": [T.replace(microsecond=250000)], "n": None})
    assert out == {
        "_id": str(oid),
        "t": "2026-03-22T23:37:05Z",
        "xs": ["2026-03-22T23:37:05.250Z"],
        "n": None,
    }
    assert iso(datetime(2026, 3, 22, 23, 37, 5)) == "2026-03-22T23:37:05Z"  # naive = UTC


def test_to_event_types():
    oid = ObjectId()
    ev = to_event(
        {"ns": {"coll": "runway_status"}, "operationType": "insert", "fullDocument": {"_id": oid}}
    )
    assert ev == {
        "type": "state",
        "doc": {"_id": str(oid)},
        "collection": "runway_status",
        "op": "insert",
    }
    ev = to_event(
        {"ns": {"coll": "flight_state"}, "operationType": "delete", "documentKey": {"_id": oid}}
    )
    assert ev["type"] == "state" and ev["op"] == "delete" and ev["doc"] == {"_id": str(oid)}
    assert to_event({"ns": {"coll": "intents_safety"}, "operationType": "insert"}) is None


def test_missing_hook_is_501(monkeypatch):
    monkeypatch.setitem(api.HOOKS, "rogue", ("holdshort.does_not_exist", "rogue", "rogue(db)"))
    with pytest.raises(api.HTTPException) as e:
        api.load_hook("rogue")
    assert e.value.status_code == 501 and "holdshort.does_not_exist" in e.value.detail


# ------------------------------------------------------------------- with Atlas


@pytest.fixture
def db(test_db):
    for a in AGENTS:
        set_permission(test_db, a, True, "test_setup")
    yield test_db
    for a in AGENTS:
        set_permission(test_db, a, True, "test_teardown")


@pytest.fixture
def api_client(db):
    with TestClient(api.create_app(db, run_gate=False)) as c:
        yield c


class Reader:
    """Reads a WebSocket in a daemon thread so the test can wait with a timeout."""

    def __init__(self, ws):
        self.q: queue.Queue = queue.Queue()
        threading.Thread(target=self._run, args=(ws,), daemon=True).start()

    def _run(self, ws):
        try:
            while True:
                self.q.put(ws.receive_json())
        except Exception:  # noqa: BLE001 - socket closed at the end of the test
            pass

    def next(self, timeout=15) -> dict:
        return self.q.get(timeout=timeout)

    def wait_for(self, pred, timeout=20) -> dict:
        deadline = time.monotonic() + timeout
        while (left := deadline - time.monotonic()) > 0:
            try:
                msg = self.q.get(timeout=left)
            except queue.Empty:
                break
            if pred(msg):
                return msg
        raise AssertionError("expected WebSocket event did not arrive")


def test_health(api_client, db):
    r = api_client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok" and body["mongo"] is True and body["db"] == db.name
    assert body["change_feed"] is True


def test_cors_localhost(api_client):
    r = api_client.get("/health", headers={"Origin": "http://localhost:5173"})
    assert r.headers.get("access-control-allow-origin") == "http://localhost:5173"
    r = api_client.get("/health", headers={"Origin": "https://evil.example"})
    assert "access-control-allow-origin" not in r.headers


def test_snapshot_then_permission_event(api_client):
    with api_client.websocket_connect("/ws") as ws:
        reader = Reader(ws)
        snap = reader.next()
        assert snap["type"] == "snapshot"
        doc = snap["doc"]
        for key in ("permissions", "rules", "transmissions", "decisions", "flight_state"):
            assert key in doc
        assert {p["agent"] for p in doc["permissions"]} >= set(AGENTS)
        assert all(isinstance(p["_id"], str) for p in doc["permissions"])
        assert all(p["changed_at"].endswith("Z") for p in doc["permissions"])

        r = api_client.post("/permissions/procedures", json={"enabled": False, "by": "CTL_TEST"})
        assert r.status_code == 200, r.text
        assert r.json()["enabled"] is False

        ev = reader.wait_for(
            lambda m: (
                m["type"] == "permission"
                and m["doc"].get("agent") == "procedures"
                and m["doc"].get("enabled") is False
            )
        )
        assert ev["doc"]["by"] == "CTL_TEST" and ev["doc"]["changed_at"].endswith("Z")


def test_permission_errors(api_client):
    r = api_client.post("/permissions/nobody", json={"enabled": True, "by": "x"})
    assert r.status_code == 400
    assert api_client.post("/permissions/safety", json={"enabled": True}).status_code == 422


def test_approve_and_revoke_end_to_end(api_client, db):
    g = Gate(db, max_await_ms=200)
    th = threading.Thread(target=g.run, daemon=True)
    th.start()
    assert g.ready.wait(20), "gate change stream did not open"
    try:
        with api_client.websocket_connect("/ws") as ws:
            reader = Reader(ws)
            assert reader.next()["type"] == "snapshot"

            intent = Intent(
                t=T,
                agent="procedures",
                action="propose_instruction",
                target=["TRUCK1"],
                runway="04",
                reason="AC8646 has passed D",
                proposal_text="TRUCK1 cross runway 04 at D after AC8646 passes",
            )
            iid = str(db.intents_procedures.insert_one(to_mongo(intent)).inserted_id)

            pending = reader.wait_for(
                lambda m: (
                    m["type"] == "decision"
                    and m["doc"]["intent_id"] == iid
                    and m["doc"]["outcome"] == "pending_approval"
                )
            )
            assert pending["doc"]["t"] == "2026-03-22T23:37:05Z"

            r = api_client.post("/approve", json={"intent_id": iid, "controller": "CTL_TEST"})
            assert r.status_code == 200, r.text
            assert r.json()["outcome"] == "executed"

            executed = reader.wait_for(
                lambda m: (
                    m["type"] == "decision"
                    and m["doc"]["intent_id"] == iid
                    and m["doc"]["outcome"] == "executed"
                )
            )
            assert executed["doc"]["approved_by"] == "CTL_TEST"
            assert executed["doc"]["rule"] == "controller_approved"

            again = api_client.post("/approve", json={"intent_id": iid, "controller": "CTL_TEST"})
            assert again.status_code == 409

            did = executed["doc"]["_id"]
            r = api_client.post("/revoke", json={"decision_id": did, "controller": "CTL_TEST"})
            assert r.status_code == 200, r.text
            revoked = reader.wait_for(
                lambda m: m["type"] == "decision" and m["doc"].get("revokes") == did
            )
            assert revoked["doc"]["outcome"] == "revoked"

            bad = api_client.post("/approve", json={"intent_id": "nope", "controller": "CTL_TEST"})
            assert bad.status_code == 422
    finally:
        g.stop()
        th.join(10)


def test_optional_endpoints_501_until_provided(api_client, monkeypatch):
    for name in ("scenario_start", "scenario_reset", "rogue", "audit"):
        mod, attr, sig = api.HOOKS[name]
        monkeypatch.setitem(api.HOOKS, name, (f"{mod}_missing_for_test", attr, sig))
    body = {"safety_enabled": True, "mode": "text"}
    assert api_client.post("/scenario/start", json=body).status_code == 501
    assert api_client.post("/scenario/reset").status_code == 501
    assert api_client.post("/rogue").status_code == 501
    r = api_client.get("/audit", params={"q": "Why was TRUCK1 held?"})
    assert r.status_code == 501 and "not implemented" in r.json()["detail"]
    bad = api_client.post("/scenario/start", json={"safety_enabled": True, "mode": "video"})
    assert bad.status_code == 422
