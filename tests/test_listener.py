"""Multi-stream listener tests (speech-to-text and LLM stubbed; own test database)."""

from __future__ import annotations

import threading
import time
from datetime import UTC, datetime, timedelta

import gridfs
import pytest

from holdshort.listener import rules
from holdshort.listener.parse import FLAG_UNCLEAR
from holdshort.listener.service import ClipProcessor, MultiStreamListener, confusable

STREAMS = {"LGA_TWR": "ctl_local", "LGA_GND": "ctl_ground", "LGA_OPS": "ctl_ground"}


@pytest.fixture
def db(test_db):
    test_db.audio_clips.delete_many({})
    test_db.transmissions.delete_many({})
    test_db.agent_state.delete_many({"_id": "listener"})
    yield test_db
    test_db.audio_clips.delete_many({})
    test_db.transmissions.delete_many({})


T0 = datetime.now(UTC).replace(microsecond=0)


def clip(db, stream_id, text, *, offset_s=0.0, audio=None, direction="in"):
    t = T0 + timedelta(seconds=offset_s)
    audio_id = None
    if audio is not None:
        audio_id = gridfs.GridFSBucket(db, "audio").upload_from_stream("clip.wav", audio)
    return db.audio_clips.insert_one({
        "tower_id": "LGA", "stream_id": stream_id, "direction": direction,
        "t_start": t, "t_end": t + timedelta(seconds=3), "duration_s": 3.0,
        "audio_id": audio_id, "mime": "audio/wav" if audio else None, "truth_text": text,
        "speaker": None, "status": "queued", "transmission_id": None, "error": None,
        "created_at": datetime.now(UTC),
    }).inserted_id


def start(db, **kw):
    kw.setdefault("llm", rules.label)
    kw.setdefault("stt", None)
    agent = MultiStreamListener(db, workers=4, **kw)
    th = threading.Thread(target=agent.run, daemon=True)
    th.start()
    assert agent.ready.wait(20)
    return agent, th


def wait_done(db, n, timeout=20.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if db.audio_clips.count_documents({"status": {"$in": ["done", "failed"]}}) >= n:
            return
        time.sleep(0.1)
    raise AssertionError("clips not processed in time")


def stop(*agents):
    for agent, _th in agents:
        agent.stop()
    for _agent, th in agents:
        th.join(15)


def tx_of(db, clip_id):
    c = db.audio_clips.find_one({"_id": clip_id})
    assert c["status"] == "done", c
    return db.transmissions.find_one({"_id": c["transmission_id"]})


def test_three_streams(db):
    ids = {
        "LGA_TWR": clip(db, "LGA_TWR",
                        "Air Canada eight six four six, runway four, cleared to land"),
        "LGA_GND": clip(db, "LGA_GND", "Truck one and company, cross runway four at delta"),
    }
    out_id = clip(db, "LGA_TWR", "not to be transcribed", direction="out")
    agent = start(db)
    try:
        # this one arrives through the change stream, not the catch-up query
        ids["LGA_OPS"] = clip(db, "LGA_OPS", "Truck one, hold short runway four")
        wait_done(db, 3)
    finally:
        stop(agent)
    for sid, cid in ids.items():
        tx = tx_of(db, cid)
        assert tx["stream_id"] == sid and tx["controller_id"] == STREAMS[sid]
        assert tx["clip_id"] == cid and tx["direction"] == "in" and tx["synthetic"] is False
    twr = tx_of(db, ids["LGA_TWR"])
    assert (twr["from"], twr["to"], twr["kind"], twr["runway"]) == ("TOWER", "AC8646",
                                                                    "clearance", "04")
    assert twr["lane_id"] == "RWY_04_22" and twr["confidence"] == 0.95 and twr["verified"]
    gnd = tx_of(db, ids["LGA_GND"])
    assert (gnd["from"], gnd["to"], gnd["instruction"], gnd["at"]) == ("GROUND", "TRUCK1",
                                                                      "cross", "D")
    assert tx_of(db, ids["LGA_OPS"])["from"] == "OPS"
    assert db.audio_clips.find_one({"_id": out_id})["status"] == "queued"
    assert db.transmissions.count_documents({}) == 3


def test_order_within_stream(db):
    seen: dict[str, list[str]] = {}
    lock = threading.Lock()

    def stt(data, mime):
        time.sleep(0.05)
        with lock:
            seen.setdefault(data.decode()[:3], []).append(data.decode())
        return data.decode()[4:], 0.9

    # inserted newest first: the listener must still take them in t_start order
    for i in reversed(range(4)):
        for sid in ("LGA_TWR", "LGA_GND"):
            clip(db, sid, None, offset_s=i,
                 audio=f"{sid[4:]}:{sid[4:]} {i} Truck one, hold short runway four".encode())
    agent = start(db, stt=stt)
    try:
        wait_done(db, 8)
    finally:
        stop(agent)
    for key in ("TWR", "GND"):
        assert [s.split()[1] for s in seen[key]] == ["0", "1", "2", "3"]
    t = [d["t"] for d in db.transmissions.find({"stream_id": "LGA_TWR"}).sort("_id", 1)]
    assert t == sorted(t)


def test_exclusive_claim_two_listeners(db):
    a = start(db)
    b = start(db)
    try:
        ids = [clip(db, sid, "Truck one, hold short runway four", offset_s=i)
               for i in range(5) for sid in STREAMS]
        wait_done(db, len(ids))
    finally:
        stop(a, b)
    for cid in ids:
        assert db.transmissions.count_documents({"clip_id": cid}) == 1
    assert a[0].done + b[0].done == len(ids)


def test_unclear_clip(db):
    proc = ClipProcessor(db, llm=rules.label, stt=None)
    empty = clip(db, "LGA_TWR", None)
    garbled = clip(db, "LGA_TWR", "Air Canada uh... say again", offset_s=1)
    for _ in range(2):
        proc.process(proc.claim("LGA_TWR"))
    for cid, conf in ((empty, 0.0), (garbled, 0.95)):
        tx = tx_of(db, cid)
        assert tx["flag"] == FLAG_UNCLEAR and tx["verified"] is False
        assert tx["confidence"] == conf


def test_readback_matched_and_mismatch(db):
    proc = ClipProcessor(db, llm=rules.label, stt=None)
    lines = [
        "Air Canada eight six four six, runway four, cleared to land",
        "Cleared to land runway four, Air Canada eight six four six",
        "Cleared to land runway one three, Air Canada eight six four six",
    ]
    ids = [clip(db, "LGA_TWR", text, offset_s=i) for i, text in enumerate(lines)]
    for _ in ids:
        proc.process(proc.claim("LGA_TWR"))
    assert [tx_of(db, c)["readback"] for c in ids] == ["missing", "matched", "mismatch"]
    # a readback on another stream has no clearance there to match
    other = clip(db, "LGA_GND", lines[1], offset_s=5)
    proc.process(proc.claim("LGA_GND"))
    assert tx_of(db, other)["readback"] == "mismatch"


def test_truth_text_fallback_when_stt_raises(db):
    def broken(data, mime):
        raise RuntimeError("speech-to-text down")

    proc = ClipProcessor(db, llm=rules.label, stt=broken)
    cid = clip(db, "LGA_GND", "Truck one and company, cross runway four at delta",
               audio=b"RIFF....")
    proc.process(proc.claim("LGA_GND"))
    tx = tx_of(db, cid)
    assert tx["confidence"] == 0.95 and tx["instruction"] == "cross" and tx["to"] == "TRUCK1"
    assert tx["audio_id"] is not None


def test_stt_text_used(db):
    proc = ClipProcessor(db, llm=rules.label, stt=lambda d, m: (d.decode(), 0.91))
    cid = clip(db, "LGA_TWR", "something else entirely",
               audio=b"Air Canada eight six four six, runway four, cleared to land")
    proc.process(proc.claim("LGA_TWR"))
    tx = tx_of(db, cid)
    assert tx["confidence"] == 0.91 and tx["instruction"] == "land"


def test_confusable():
    assert confusable("AC8646", "AC8464")
    assert confusable("AC8646", "AC8645")
    assert not confusable("AC8646", "AC1234")
    assert not confusable("AC8646", "DL8646")
    assert not confusable("TRUCK1", "TRUCK1")
