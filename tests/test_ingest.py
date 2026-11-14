"""/ingest/* against the test database (docs/v2/SIMULATOR_CONTRACT.md)."""

import json
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import pytest
from bson import ObjectId
from fastapi import FastAPI
from fastapi.testclient import TestClient
from gridfs import GridFSBucket

from holdshort.api.routers.ingest import router

D_CROSSING = (-73.8783, 40.7756)  # lon, lat (world_lga points.D_CROSSING)
HP_D_SE = (-73.8775, 40.775)


def iso(dt=None):
    return (dt or datetime.now(UTC)).isoformat().replace("+00:00", "Z")


@pytest.fixture(scope="module")
def app(test_db):
    a = FastAPI()
    a.state.db = test_db
    a.include_router(router)
    return a


@pytest.fixture
def api(app):
    return TestClient(app)


def obj(oid, lon, lat, **kw):
    return {"object_id": oid, "kind": "vehicle", "callsign": oid, "status": "ground",
            "lat": lat, "lon": lon, **kw}


def test_world(api):
    r = api.get("/ingest/world", params={"tower_id": "LGA"})
    assert r.status_code == 200
    body = r.json()
    assert body["tower"]["_id"] == "LGA"
    assert {lane["_id"] for lane in body["lanes"]} >= {"RWY_04_22", "TWY_D"}
    assert all(lane["area"]["type"] == "Polygon" for lane in body["lanes"])
    assert "D_CROSSING" in body["points"]
    assert api.get("/ingest/world", params={"tower_id": "XXX"}).status_code == 404


def test_positions_lanes_and_history(api, test_db):
    test_db.objects.update_one({"_id": "TRUCK1"}, {"$set": {"clearance": {"x": 1},
                                                            "hold_short": True}}, upsert=True)
    t = datetime.now(UTC)
    r = api.post("/ingest/positions", json={"tower_id": "LGA", "t": iso(t), "objects": [
        obj("TRUCK1", *D_CROSSING, label="Fire truck", heading_deg=314, speed_kt=10),
        obj("TRUCK2", *HP_D_SE),
        {"object_id": "AC8646", "kind": "aircraft", "callsign": "AC8646", "label": "A320",
         "status": "airborne", "lat": D_CROSSING[1], "lon": D_CROSSING[0], "alt_ft": 1200},
        {"object_id": "AC8464", "kind": "aircraft", "callsign": "AC8464",
         "status": "airborne", "lat": D_CROSSING[1], "lon": D_CROSSING[0], "alt_ft": 50},
        {"object_id": "OBJ-17", "kind": "person", "status": "ground",
         "lat": 40.70, "lon": -73.95},
    ]})
    assert r.status_code == 200, r.text
    assert r.json() == {"accepted": 5, "lanes": {
        "TRUCK1": "RWY_04_22", "TRUCK2": "TWY_D", "AC8646": None, "AC8464": "RWY_04_22",
        "OBJ-17": None}}
    truck = test_db.objects.find_one({"_id": "TRUCK1"})
    assert truck["clearance"] == {"x": 1} and truck["hold_short"] is True  # untouched
    assert truck["lane_id"] == "RWY_04_22" and truck["label"] == "Fire truck"
    assert truck["position"] == {"type": "Point", "coordinates": list(D_CROSSING)}
    ttl = (truck["expires_at"] - truck["updated_at"]).total_seconds()
    assert ttl == pytest.approx(120, abs=1)
    new = test_db.objects.find_one({"_id": "TRUCK2"})
    assert new["clearance"] is None and new["hold_short"] is False
    samples = list(test_db.positions.find({"meta.object_id": "TRUCK1",
                                           "t": {"$gte": t - timedelta(seconds=1)}}))
    assert len(samples) == 1 and samples[0]["lane_id"] == "RWY_04_22"
    assert samples[0]["meta"] == {"object_id": "TRUCK1", "tower_id": "LGA",
                                  "kind": "vehicle", "callsign": "TRUCK1"}


def test_positions_ttl_env(api, test_db, monkeypatch):
    monkeypatch.setenv("OBJECT_TTL_S", "30")
    assert api.post("/ingest/positions",
                    json={"objects": [obj("TRUCK9", *HP_D_SE)]}).status_code == 200
    d = test_db.objects.find_one({"_id": "TRUCK9"})
    assert (d["expires_at"] - d["updated_at"]).total_seconds() == pytest.approx(30, abs=1)


def test_positions_errors(api):
    p = "/ingest/positions"
    r = api.post(p, json={"objects": [{"object_id": "X", "kind": "boat", "status": "ground",
                                       "lat": 1, "lon": 1}]})
    assert r.status_code == 400 and "kind" in r.json()["detail"]
    r = api.post(p, json={"objects": [{"object_id": "X", "kind": "vehicle", "status": "ground",
                                       "lon": 1}]})
    assert r.status_code == 400 and "lat" in r.json()["detail"]
    assert api.post(p, content=b"{nope", headers={"content-type": "application/json"}
                    ).status_code == 400
    assert api.post(p, json={"tower_id": "XXX", "objects": []}).status_code == 404
    old = iso(datetime.now(UTC) - timedelta(seconds=300))
    assert api.post(p, json={"t": old, "objects": [obj("T1", 0, 0)]}).status_code == 422
    big = [obj(f"V{i}", 0, 0) for i in range(501)]
    assert api.post(p, json={"objects": big}).status_code == 400


def test_positions_200_batch_fast(api, test_db):
    lon0, lat0 = -73.885, 40.770
    objs = [obj(f"BATCH{i}", lon0 + i * 1e-5, lat0 + i * 1e-5, speed_kt=5)
            for i in range(200)]
    api.post("/ingest/positions", json={"objects": objs[:1]})  # warm caches
    t0 = time.perf_counter()
    r = api.post("/ingest/positions", json={"objects": objs})
    elapsed = time.perf_counter() - t0
    assert r.status_code == 200 and r.json()["accepted"] == 200
    assert len(r.json()["lanes"]) == 200
    assert test_db.objects.count_documents({"_id": {"$regex": "^BATCH"}}) == 200
    assert elapsed < 1.0, f"200 objects took {elapsed:.2f}s"


def test_remove(api, test_db):
    api.post("/ingest/positions", json={"objects": [obj("TRUCK5", *HP_D_SE)]})
    r = api.post("/ingest/objects/TRUCK5/remove", json={"tower_id": "LGA", "reason": "gate"})
    assert r.status_code == 200 and r.json() == {"removed": True}
    assert test_db.objects.find_one({"_id": "TRUCK5"}) is None
    ev = test_db.state_events.find_one({"kind": "object_removed", "ref": "TRUCK5"})
    assert ev and ev["doc"]["reason"] == "gate" and ev["tower_id"] == "LGA"
    assert api.post("/ingest/objects/TRUCK5/remove", json={}).status_code == 404


def _meta(stream="LGA_TWR", **kw):
    return json.dumps({"tower_id": "LGA", "stream_id": stream, "t_start": iso(),
                       "duration_s": 2.0, "speaker": "TOWER",
                       "truth_text": f"test on {stream}", **kw})


def test_audio_concurrent_three_streams(app, test_db):
    streams = ["LGA_TWR", "LGA_GND", "LGA_OPS"]

    def post(stream):
        with TestClient(app) as c:
            payload = f"RIFF-{stream}".encode() * 100
            return stream, payload, c.post("/ingest/audio", data={"meta": _meta(stream)},
                                           files={"file": ("a.wav", payload, "audio/wav")})

    with ThreadPoolExecutor(3) as ex:
        results = list(ex.map(post, streams))
    bucket = GridFSBucket(test_db, bucket_name="audio")
    for stream, payload, r in results:
        assert r.status_code == 202, r.text
        clip = test_db.audio_clips.find_one({"_id": ObjectId(r.json()["clip_id"])})
        assert clip["status"] == "queued" and clip["direction"] == "in"
        assert clip["stream_id"] == stream and clip["mime"] == "audio/wav"
        assert clip["truth_text"] == f"test on {stream}"
        assert (clip["t_end"] - clip["t_start"]).total_seconds() == pytest.approx(2.0)
        assert bucket.open_download_stream(clip["audio_id"]).read() == payload
        f = test_db["audio.files"].find_one({"_id": clip["audio_id"]})
        assert f["metadata"]["stream_id"] == stream and f["metadata"]["direction"] == "in"
        assert f["metadata"]["tower_id"] == "LGA" and f["metadata"]["t_start"]


def test_audio_errors(api):
    wav = ("a.wav", b"RIFF0000", "audio/wav")
    p = "/ingest/audio"
    assert api.post(p, data={"meta": _meta(duration_s=61)},
                    files={"file": wav}).status_code == 413
    big = ("a.wav", b"0" * (5 * 1024 * 1024 + 1), "audio/wav")
    assert api.post(p, data={"meta": _meta()}, files={"file": big}).status_code == 413
    assert api.post(p, data={"meta": _meta("NOPE")}, files={"file": wav}).status_code == 404
    late = iso(datetime.now(UTC) + timedelta(seconds=200))
    assert api.post(p, data={"meta": _meta(t_start=late)},
                    files={"file": wav}).status_code == 422
    assert api.post(p, data={"meta": "{bad"}, files={"file": wav}).status_code == 400
    assert api.post(p, data={"meta": _meta()}).status_code == 400


def test_transcripts(api, test_db):
    r = api.post("/ingest/transcripts", json={
        "tower_id": "LGA", "stream_id": "LGA_GND", "t_start": iso(),
        "text": "Truck one, cross runway four at Delta.", "confidence": 0.9})
    assert r.status_code == 202, r.text
    clip = test_db.audio_clips.find_one({"_id": ObjectId(r.json()["clip_id"])})
    assert clip["audio_id"] is None and clip["status"] == "queued"
    assert clip["truth_text"] == "Truck one, cross runway four at Delta."
    assert clip["direction"] == "in" and clip["duration_s"] > 0
    base = {"stream_id": "LGA_GND", "t_start": iso(), "text": "x"}
    assert api.post("/ingest/transcripts", json={**base, "stream_id": "NOPE"}
                    ).status_code == 404
    assert api.post("/ingest/transcripts", json={**base, "t_start": iso(
        datetime.now(UTC) - timedelta(seconds=121))}).status_code == 422
    assert api.post("/ingest/transcripts", json={"stream_id": "LGA_GND"}).status_code == 400


def test_weather(api, test_db):
    r = api.post("/ingest/weather", json={"tower_id": "LGA", "visibility_sm": 0.75,
                                          "wind": "03012KT"})
    assert r.status_code == 200
    w = test_db.weather.find_one({"_id": ObjectId(r.json()["weather_id"])})
    assert w["visibility_sm"] == 0.75 and w["raw"] is None
    assert api.post("/ingest/weather", json={"visibility_sm": -1}).status_code == 400


def test_streams_upsert(api, test_db):
    body = {"id": "LGA_APP", "tower_id": "LGA", "name": "Approach 120.8", "kind": "approach",
            "frequency_mhz": 120.8, "controller_id": "ctl_local", "station": "APPROACH"}
    assert api.post("/ingest/streams", json=body).status_code == 200
    assert api.post("/ingest/streams", json={**body, "name": "App"}).status_code == 200
    assert test_db.streams.find_one({"_id": "LGA_APP"})["name"] == "App"
    r = api.post("/ingest/transcripts", json={"stream_id": "LGA_APP", "t_start": iso(),
                                              "text": "hello"})
    assert r.status_code == 202
    assert api.post("/ingest/streams", json={**body, "kind": "x"}).status_code == 400
    assert api.post("/ingest/streams", json={**body, "controller_id": "nobody"}
                    ).status_code == 404


def test_runs(api, test_db):
    r = api.post("/ingest/runs", json={"tower_id": "LGA", "scenario": "laguardia_crossing",
                                       "kind": "incident", "note": "test"})
    assert r.status_code == 200
    run_id = r.json()["run_id"]
    assert test_db.sim_runs.find_one({"_id": ObjectId(run_id)})["ended_at"] is None
    assert api.post(f"/ingest/runs/{run_id}/end").status_code == 200
    assert test_db.sim_runs.find_one({"_id": ObjectId(run_id)})["ended_at"] is not None
    assert api.post(f"/ingest/runs/{ObjectId()}/end").status_code == 404
    assert api.post("/ingest/runs/nope/end").status_code == 404
    assert api.post("/ingest/runs", json={"scenario": "x", "kind": "weird"}).status_code == 400


def test_reset_live(api, test_db):
    api.post("/ingest/positions", json={"objects": [obj("TRUCK7", *HP_D_SE)]})
    test_db.context.insert_one({"tower_id": "LGA", "kind": "note", "summary": "x",
                                "t": datetime.now(UTC), "expires_at": datetime.now(UTC)})
    test_db.lane_status.update_one({"_id": "RWY_04_22"}, {"$set": {
        "state": "conflict", "occupants": ["TRUCK7"], "reason": "test"}})
    before = test_db.state_events.count_documents({"kind": "lane_status"})
    r = api.post("/ingest/reset", json={"tower_id": "LGA", "scope": "live"})
    assert r.status_code == 200 and r.json()["lanes_cleared"] == ["RWY_04_22"]
    assert test_db.objects.count_documents({}) == 0
    assert test_db.context.count_documents({}) == 0
    ls = test_db.lane_status.find_one({"_id": "RWY_04_22"})
    assert ls["state"] == "clear" and ls["occupants"] == [] and ls["reason"] is None
    assert test_db.state_events.count_documents({"kind": "lane_status"}) == before + 1
    assert test_db.transmissions is not None  # permanent log untouched
    assert api.post("/ingest/reset", json={"scope": "all"}).status_code == 400


def test_ingest_key(api, monkeypatch):
    monkeypatch.setenv("INGEST_KEY", "s3cret")
    assert api.get("/ingest/world").status_code == 401
    assert api.get("/ingest/world", headers={"X-Ingest-Key": "bad"}).status_code == 401
    assert api.get("/ingest/world", headers={"X-Ingest-Key": "s3cret"}).status_code == 200
    monkeypatch.delenv("INGEST_KEY")
    assert api.get("/ingest/world").status_code == 200
