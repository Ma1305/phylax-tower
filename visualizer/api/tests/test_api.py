"""Every /viz route against a scratch database seeded by seed_demo (dropped at the end)."""

from __future__ import annotations

import os
import re
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from bson import ObjectId
from fastapi.testclient import TestClient
from pymongo import MongoClient

API_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(API_DIR))

import config  # noqa: E402
import seed_demo  # noqa: E402

BRANCH = subprocess.run(
    ["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=API_DIR, capture_output=True, text=True
).stdout
TASK = (re.search(r"hq/(\d+)-", BRANCH) or [None, os.environ.get("HQ_TASK", "30")])[1]
TEST_DB = f"holdshort_v2_t{TASK}"
assert TEST_DB not in config.PROTECTED_DBS


def iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat().replace("+00:00", "Z")


@pytest.fixture(scope="session")
def mongo():
    client = MongoClient(config.mongo_uri(), tz_aware=True)
    yield client
    client.drop_database(TEST_DB)
    client.close()


@pytest.fixture(scope="session")
def seeded(mongo):
    return seed_demo.seed(mongo[TEST_DB])


@pytest.fixture(scope="session")
def api(seeded):
    os.environ["MONGODB_DB"] = TEST_DB
    import app as viz_app

    with TestClient(viz_app.app) as client:
        yield client


def get(api, path, **params):
    r = api.get(path, params=params)
    assert r.status_code == 200, r.text
    return r.json()


def test_health(api):
    body = get(api, "/viz/health")
    assert body["ok"] is True and body["db"] == TEST_DB


def test_world(api):
    w = get(api, "/viz/world", tower_id="LGA", controller_id="ctl_local")
    assert w["tower"]["_id"] == "LGA"
    assert len(w["lanes"]) == 6 and len(w["hold_points"]) == 4
    assert len(w["controllers"]) == 3 and len(w["streams"]) == 3
    rwy = next(x for x in w["lanes"] if x["_id"] == "RWY_04_22")
    ring = rwy["area"]["coordinates"][0]
    assert rwy["area"]["type"] == "Polygon" and ring[0] == ring[-1] and len(ring) == 5
    assert rwy["mine"] is True
    assert next(x for x in w["lanes"] if x["_id"] == "TWY_B")["mine"] is False
    assert [s["_id"] for s in w["streams"] if s["mine"]] == ["LGA_TWR"]
    assert api.get("/viz/world", params={"tower_id": "NOPE"}).status_code == 404


def test_bounds(api, seeded):
    b = get(api, "/viz/bounds")
    assert b["from"] == iso(seeded["from"]).replace("Z", ".000Z")
    assert datetime.fromisoformat(b["to"].replace("Z", "+00:00")) >= seeded["to"]


def test_snapshot_now(api, seeded):
    s = get(api, "/viz/snapshot", controller_id="ctl_local")
    assert s["live"] is True
    assert {o["_id"] for o in s["objects"]} == set(seeded["objects"])
    assert {o["kind"] for o in s["objects"]} == {"aircraft", "vehicle", "person", "unknown"}
    lanes = {x["_id"]: x for x in s["lane_status"]}
    assert lanes["RWY_04_22"]["state"] == "occupied" and lanes["RWY_04_22"]["mine"] is True
    assert lanes["TWY_D"]["mine"] is False
    assert [d["_id"] for d in s["pending"]] == [str(seeded["pending_decision_id"])]
    assert s["pending"][0]["intent"]["action"] == "propose_instruction"
    assert s["pending"][0]["mine"] is True
    assert len(s["decisions"]) == 8 and len(s["transmissions"]) == 11
    assert s["weather"]["visibility_sm"] == 2
    perms = {p["_id"]: p for p in s["permissions"]}
    assert len(perms) == 5 and perms["weather"]["tier"] == "needs_approval"
    by_id = {d["_id"]: d for d in s["decisions"]}
    # stops are autonomous by default: executed without a controller, and still in effect
    stop = by_id[str(seeded["stop_decision_id"])]
    assert stop["outcome"] == "executed" and stop["rule"] == "tier_autonomous"
    assert stop["approved_by"] is None and stop["intent"]["reason"]
    assert not any(d["revokes"] == stop["_id"] for d in s["decisions"])
    spoken = [t for t in s["transmissions"] if t["decision_id"] == stop["_id"]]
    assert len(spoken) == 1 and spoken[0]["synthetic"] and spoken[0]["from"] == "HOLDSHORT"
    lifted = [d for d in s["decisions"] if d["revokes"] == str(seeded["lifted_stop_decision_id"])]
    assert [(d["outcome"], d["rule"], d["approved_by"]) for d in lifted] == [
        ("revoked", "controller_revoked", "ctl_ground")
    ]
    assert by_id[str(seeded["rejected_decision_id"])]["rule"] == "mode_reactive"
    assert {d["outcome"] for d in s["decisions"]} == {
        "executed",
        "pending_approval",
        "rejected",
        "revoked",
    }


def test_snapshot_in_the_past_differs(api, seeded):
    now = get(api, "/viz/snapshot")
    past = get(api, "/viz/snapshot", t=iso(seeded["from"] + timedelta(seconds=50)))
    assert past["live"] is False
    lanes = {x["_id"]: x for x in past["lane_status"]}
    assert len(lanes) == 6
    assert lanes["RWY_04_22"]["state"] == "reserved"  # the arrival had not landed yet
    assert lanes["TWY_D"]["state"] == "clear"
    assert past["pending"] == [] and past["decisions"] == []
    assert len(past["transmissions"]) == 4
    assert past["weather"]["visibility_sm"] == 10
    assert {p["_id"]: p["tier"] for p in past["permissions"]}["weather"] == "autonomous"
    then = {o["_id"]: o for o in past["objects"]}
    cur = {o["_id"]: o for o in now["objects"]}
    assert set(then) == set(cur)
    assert then["AC8646"]["status"] == "airborne" and cur["AC8646"]["status"] == "ground"
    assert then["AC8646"]["position"] != cur["AC8646"]["position"]
    assert then["AC8646"]["lane_id"] is None and cur["AC8646"]["lane_id"] == "RWY_04_22"

    # a stop from a needs_approval agent waits for the controller: then it is the pending item
    mid = get(api, "/viz/snapshot", t=iso(seeded["from"] + timedelta(seconds=108)))
    assert [d["_id"] for d in mid["pending"]] == [str(seeded["stop_pending_decision_id"])]
    # before anything was recorded there is nothing to show
    empty = get(api, "/viz/snapshot", t=iso(seeded["from"] - timedelta(seconds=120)))
    assert empty["objects"] == [] and empty["lane_status"] == [] and empty["weather"] is None
    assert api.get("/viz/snapshot", params={"t": "yesterday"}).status_code == 400


def test_tracks(api, seeded):
    args = {"from": iso(seeded["from"]), "to": iso(seeded["to"])}
    full = get(api, "/viz/tracks", **args)
    assert set(full["objects"]) == set(seeded["objects"])
    ac = full["objects"]["AC8646"]
    assert ac["kind"] == "aircraft" and ac["callsign"] == "AC8646" and len(ac["points"]) == 181
    assert len(ac["points"][0]) == 5 and ac["points"][0][0] < ac["points"][-1][0]
    assert ac["points"][0][3] > ac["points"][-1][3] == 0
    coarse = get(api, "/viz/tracks", step_s=10, **args)
    assert 18 <= len(coarse["objects"]["AC8646"]["points"]) <= 19
    part = get(
        api,
        "/viz/tracks",
        **{"from": args["from"], "to": iso(seeded["from"] + timedelta(seconds=30))},
    )
    assert len(part["objects"]["OPS7"]["points"]) == 31


def test_timeline(api, seeded):
    marks = get(api, "/viz/timeline", **{"from": iso(seeded["from"]), "to": iso(seeded["to"])})[
        "marks"
    ]
    kinds = [m["type"] for m in marks]
    assert kinds.count("transmission") == 11 and kinds.count("run") == 2
    assert kinds.count("permission") == 7
    assert kinds.count("alert") + kinds.count("decision") == 8 and "alert" in kinds
    assert [m["t"] for m in marks] == sorted(m["t"] for m in marks)
    assert all({"t", "type", "stream_id", "severity", "label"} <= set(m) for m in marks)
    early = get(
        api,
        "/viz/timeline",
        **{
            "from": iso(seeded["from"] + timedelta(seconds=5)),
            "to": iso(seeded["from"] + timedelta(seconds=20)),
        },
    )
    assert [m["type"] for m in early["marks"]] == ["transmission", "transmission"]


def test_stream_clips(api, seeded):
    args = {"from": iso(seeded["from"]), "to": iso(seeded["to"])}
    counts = {}
    for stream in ("LGA_TWR", "LGA_GND", "LGA_OPS"):
        clips = get(api, f"/viz/streams/{stream}/clips", **args)["clips"]
        counts[stream] = len(clips)
        assert all(c["has_audio"] and c["transmission"]["stream_id"] == stream for c in clips)
        assert {"clip_id", "t_start", "t_end", "duration_s", "direction"} <= set(clips[0])
    assert counts == {"LGA_TWR": 3, "LGA_GND": 3, "LGA_OPS": 5}
    ops = get(api, "/viz/streams/LGA_OPS/clips", **args)["clips"]
    spoken = [c for c in ops if c["direction"] == "out"]
    assert len(spoken) == 2 and spoken[0]["transmission"]["synthetic"] is True
    assert get(api, "/viz/streams/NOPE/clips", **args)["clips"] == []


def test_audio_and_range(api, seeded):
    clip_id = str(seeded["clip_ids"][0])
    whole = api.get(f"/viz/audio/{clip_id}")
    assert whole.status_code == 200
    assert whole.headers["content-type"] == "audio/wav"
    assert whole.headers["accept-ranges"] == "bytes"
    assert whole.content[:4] == b"RIFF" and len(whole.content) > 1000
    total = len(whole.content)

    part = api.get(f"/viz/audio/{clip_id}", headers={"Range": "bytes=100-199"})
    assert part.status_code == 206
    assert part.headers["content-range"] == f"bytes 100-199/{total}"
    assert part.content == whole.content[100:200]
    tail = api.get(f"/viz/audio/{clip_id}", headers={"Range": "bytes=-50"})
    assert tail.status_code == 206 and tail.content == whole.content[-50:]
    rest = api.get(f"/viz/audio/{clip_id}", headers={"Range": "bytes=0-"})
    assert rest.status_code == 206 and len(rest.content) == total
    assert api.get(f"/viz/audio/{clip_id}", headers={"Range": f"bytes={total}-"}).status_code == 416
    assert api.get(f"/viz/audio/{ObjectId()}").status_code == 404
    assert api.get("/viz/audio/not-an-id").status_code == 404


def test_cors(api):
    for origin in ("http://localhost:5180", "http://127.0.0.1:18555"):
        r = api.get("/viz/health", headers={"Origin": origin})
        assert r.headers["access-control-allow-origin"] == origin
    r = api.get("/viz/health", headers={"Origin": "http://evil.example"})
    assert "access-control-allow-origin" not in r.headers


def test_live_snapshot_then_change(api, mongo, seeded):
    deadline = time.time() + 20
    while not get(api, "/viz/health")["live"]:
        assert time.time() < deadline, "change stream did not open"
        time.sleep(0.2)
    db = mongo[TEST_DB]
    pool = ThreadPoolExecutor(max_workers=1)

    def next_of(ws, wanted):
        def read():
            while True:
                msg = ws.receive_json()
                if msg["type"] == wanted:
                    return msg

        return pool.submit(read).result(timeout=20)

    with api.websocket_connect("/viz/live?tower_id=LGA") as ws:
        first = ws.receive_json()
        assert first["type"] == "snapshot" and first["doc"]["live"] is True
        assert len(first["doc"]["objects"]) == 6

        now = datetime.now(UTC)
        tx_id = db.transmissions.insert_one(
            {
                "tower_id": "LGA",
                "stream_id": "LGA_TWR",
                "controller_id": "ctl_local",
                "t": now,
                "t_end": now,
                "direction": "in",
                "from": "TOWER",
                "to": "AC8646",
                "kind": "other",
                "instruction": "none",
                "transcript": "AC8646, exit at the end.",
                "confidence": 0.9,
                "readback": "not_applicable",
                "verified": True,
                "flag": None,
                "synthetic": False,
            }
        ).inserted_id
        msg = next_of(ws, "transmission")
        assert msg["doc"]["_id"] == str(tx_id) and msg["doc"]["t"].endswith("Z")

        # a decision arrives with its intent merged in
        intent = db.intents_safety.find_one({"reason": "no_crossing_during_active_landing"})
        dec_id = db.decisions.insert_one(
            {
                "t": datetime.now(UTC),
                "tower_id": "LGA",
                "intent_id": intent["_id"],
                "agent": "safety",
                "intent_action": "stop",
                "severity": "critical",
                "outcome": "revoked",
                "rule": "controller_revoked",
                "lane_id": "RWY_04_22",
                "controller_id": "ctl_local",
                "stream_id": "LGA_OPS",
                "text": None,
                "target": [],
                "mode": "proactive",
                "approved_by": None,
                "revokes": seeded["stop_decision_id"],
                "notified": [],
            }
        ).inserted_id
        msg = next_of(ws, "decision")
        assert msg["doc"]["_id"] == str(dec_id)
        assert msg["doc"]["intent"]["reason"] == "no_crossing_during_active_landing"

        # twenty quick position updates of one object are coalesced to at most 2 per second
        started = time.time()
        for i in range(20):
            db.objects.update_one({"_id": "OPS7"}, {"$set": {"speed_kt": 30 + i}})
        db.lane_status.update_one({"_id": "TWY_G"}, {"$set": {"state": "closed"}})
        seen = []
        while True:
            msg = pool.submit(ws.receive_json).result(timeout=20)
            if msg["type"] == "object":
                seen.append(msg["doc"])
            if msg["type"] == "lane_status":
                assert msg["doc"]["_id"] == "TWY_G" and msg["doc"]["state"] == "closed"
                break
        final = next_of(ws, "object")["doc"] if not seen or seen[-1]["speed_kt"] != 49 else seen[-1]
        elapsed = time.time() - started
        assert final["_id"] == "OPS7" and final["speed_kt"] == 49  # the newest state wins
        assert len(seen) + 1 <= 2 * elapsed + 2

        db.objects.delete_one({"_id": "OBJ-23"})
        assert next_of(ws, "object_removed")["doc"] == {"_id": "OBJ-23"}
    pool.shutdown(wait=False)


def test_seed_refuses_real_databases(mongo):
    for name in ("holdshort", "holdshort_v2"):
        assert seed_demo.main(["--db", name]) == 2
        with pytest.raises(SystemExit):
            seed_demo.seed(mongo[name])
