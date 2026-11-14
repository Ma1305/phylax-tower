"""Speaker + GET /ingest/outbound (text-to-speech stubbed; own test database)."""

import threading
import time
from datetime import UTC, datetime, timedelta

import gridfs
import pytest
from bson import ObjectId
from fastapi import FastAPI
from fastapi.testclient import TestClient

from holdshort.api.routers.outbound import router
from holdshort.contracts import Decision, to_mongo
from holdshort.speaker import core
from holdshort.speaker.core import Speaker, speak_decision

FAKE_MP3 = b"ID3fake-mp3-bytes" * 100


@pytest.fixture
def db(test_db, monkeypatch):
    monkeypatch.setattr(core, "voice_enabled", lambda: True)
    monkeypatch.setattr(core, "tts_elevenlabs", lambda text: (FAKE_MP3, "audio/mpeg"))
    for c in ("decisions", "transmissions", "audio_clips", "audio.files", "audio.chunks"):
        test_db[c].delete_many({})
    test_db.agent_state.delete_many({"_id": "speaker"})
    return test_db


STOP_TEXT = "Truck one, STOP. Hold short runway zero four."


def decision(action="stop", outcome="executed", text=STOP_TEXT, approved_by=None, **kw):
    d = Decision(
        t=datetime.now(UTC), tower_id="LGA", intent_id=ObjectId(), agent="safety",
        intent_action=action, severity="critical", outcome=outcome, rule="runway_incursion",
        lane_id="RWY_04_22", controller_id="ctl_local", stream_id="LGA_GND", text=text,
        target=["TRUCK1", "AC8646"], mode="proactive",
        authorized_by={"permission_id": "safety"}, approved_by=approved_by, **kw)
    return to_mongo(d)


def insert(db, doc):
    doc["_id"] = db.decisions.insert_one(doc).inserted_id
    return doc


def start(db):
    sp = Speaker(db, max_await_ms=100)
    th = threading.Thread(target=sp.run, daemon=True)
    th.start()
    assert sp.ready.wait(20)
    return sp, th


def stop(sp, th):
    sp.stop()
    th.join(10)


def wait_for(fn, timeout=15):
    end = time.time() + timeout
    while time.time() < end:
        if fn():
            return True
        time.sleep(0.2)
    return fn()


def test_executed_stop_is_spoken_once(db):
    d = insert(db, decision())
    tx = speak_decision(db, d)
    assert tx is not None
    assert speak_decision(db, d) is None  # exactly once
    txs = list(db.transmissions.find({"decision_id": d["_id"]}))
    assert len(txs) == 1
    tx = txs[0]
    assert tx["from"] == "HOLDSHORT" and tx["to"] == "TRUCK1"
    assert tx["kind"] == "stop" and tx["instruction"] == "hold_short"
    assert tx["synthetic"] and tx["verified"] and tx["confidence"] == 1
    assert tx["readback"] == "not_applicable" and tx["direction"] == "out"
    assert tx["stream_id"] == "LGA_GND" and tx["lane_id"] == "RWY_04_22"
    assert tx["transcript"] == d["text"]
    clips = list(db.audio_clips.find({}))
    assert len(clips) == 1
    clip = clips[0]
    assert clip["_id"] == tx["clip_id"] and clip["transmission_id"] == tx["_id"]
    assert clip["direction"] == "out" and clip["status"] == "done"
    assert clip["truth_text"] == d["text"] and clip["stream_id"] == "LGA_GND"
    bucket = gridfs.GridFSBucket(db, bucket_name="audio")
    data = bucket.open_download_stream(clip["audio_id"]).read()
    assert data == FAKE_MP3


def test_placeholder_when_voice_off(db, monkeypatch):
    monkeypatch.setattr(core, "voice_enabled", lambda: False)
    d = insert(db, decision())
    tx = speak_decision(db, d)
    data = gridfs.GridFSBucket(db, bucket_name="audio").open_download_stream(tx["audio_id"]).read()
    assert data[:4] == b"RIFF" and len(data) > 1000
    assert db.audio_clips.find_one({"_id": tx["clip_id"]})["duration_s"] > 0.3


@pytest.mark.parametrize("doc", [
    dict(action="warn"),
    dict(outcome="rejected"),
    dict(outcome="pending_approval"),
    dict(outcome="revoked"),
    dict(text=None),
    dict(action="propose_instruction", outcome="pending_approval"),
])
def test_never_spoken(db, doc):
    d = insert(db, decision(**doc))
    assert speak_decision(db, d) is None
    assert db.transmissions.count_documents({}) == 0
    assert db.audio_clips.count_documents({}) == 0


def test_stream_warn_nothing_and_proposal_once_across_restart(db):
    sp, th = start(db)
    # remember a resume point before the proposal, to replay it after the restart
    with db.watch([{"$match": {"ns.coll": "decisions"}}], max_await_time_ms=100) as cs:
        cs.try_next()
        before = cs.resume_token
    insert(db, decision(action="warn", text="Caution, traffic."))
    p = insert(db, decision(action="propose_instruction", approved_by="ctl_local",
                            text="Air Canada eight six four six, go around."))
    assert wait_for(lambda: db.transmissions.count_documents({}) == 1)
    stop(sp, th)
    tx = db.transmissions.find_one({})
    assert tx["decision_id"] == p["_id"]
    assert tx["kind"] == "other" and tx["instruction"] == "none"

    # restart from a token before the proposal: it is seen again but not spoken again
    db.agent_state.update_one({"_id": "speaker"}, {"$set": {"resume_token": before}})
    sp, th = start(db)
    assert sp.resumed
    assert wait_for(lambda: sp.processed >= 1)
    time.sleep(0.5)
    stop(sp, th)
    assert db.transmissions.count_documents({}) == 1
    assert db.audio_clips.count_documents({"transmission_id": tx["_id"]}) == 1


def test_outbound_endpoint(db):
    app = FastAPI()
    app.state.db = db
    app.include_router(router)
    c = TestClient(app)
    d = insert(db, decision(action="propose_instruction", approved_by="ctl_local",
                            text="Truck one, hold position."))
    t0 = (datetime.now(UTC) - timedelta(seconds=5)).isoformat().replace("+00:00", "Z")
    tx = speak_decision(db, d)

    r = c.get("/ingest/outbound", params={"tower_id": "LGA", "since": t0})
    assert r.status_code == 200
    body = r.json()
    assert body["now"].endswith("Z")
    assert len(body["calls"]) == 1
    call = body["calls"][0]
    assert call == {
        "id": str(tx["_id"]), "t": call["t"], "decision_id": str(d["_id"]),
        "stream_id": "LGA_GND",
        "action": "propose_instruction", "target": ["TRUCK1", "AC8646"],
        "lane_id": "RWY_04_22", "text": "Truck one, hold position.",
        "approved_by": "ctl_local", "clip_id": str(tx["clip_id"]),
    }
    r2 = c.get("/ingest/outbound", params={"tower_id": "LGA", "since": body["now"]})
    # the 3 s overlap repeats the call; clients de-duplicate by decision_id
    assert [x["decision_id"] for x in r2.json()["calls"]] == [str(d["_id"])]
    late = (datetime.now(UTC) + timedelta(seconds=10)).isoformat().replace("+00:00", "Z")
    assert c.get("/ingest/outbound", params={"tower_id": "LGA", "since": late}).json()["calls"] == []
    assert c.get("/ingest/outbound", params={"tower_id": "XXX", "since": t0}).json()["calls"] == []
    assert c.get("/ingest/outbound", params={"tower_id": "LGA", "since": "bad"}).status_code == 400
