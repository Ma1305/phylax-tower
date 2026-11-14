"""The stub tower answers the way docs/v2/SIMULATOR_CONTRACT.md says the tower does."""

from __future__ import annotations

import json
import re
import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import httpx
import pytest
import uvicorn
from fastapi.testclient import TestClient

from hssim.stub_tower import StubStore, create_stub_app
from hssim.world import World

ISO_Z = re.compile(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(\.\d+)?Z")
MB = 1024 * 1024
WAV = b"RIFF" + bytes(2000)
LANDING = "Air Canada eight six four six, runway four, cleared to land."
CROSSING = "Truck one and company, cross runway four at Delta."
STOP_TEXT = "Truck one, STOP. Hold short runway zero four."
CALL_KEYS = {"t", "decision_id", "stream_id", "action", "target", "lane_id", "text",
             "approved_by", "clip_id"}
CLIP_KEYS = {"clip_id", "kind", "stream_id", "t_start", "duration_s", "speaker", "text",
             "size_bytes", "mime", "received_at"}


# ------------------------------------------------------------------------- helpers
@pytest.fixture(scope="module")
def world() -> World:
    return World.load_default()


@pytest.fixture
def app(world):
    return create_stub_app(world)


@pytest.fixture
def client(app) -> TestClient:
    return TestClient(app)


@pytest.fixture
def store(app) -> StubStore:
    return app.state.store


class Clock:
    """A clock that only moves when the test moves it; put in place of ``store.now``.

    The helpers below stamp what they send with the real time, so keep it well within
    120 s of the real clock while a test is still posting.
    """

    def __init__(self):
        self.t = datetime.now(UTC)

    def __call__(self) -> datetime:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += timedelta(seconds=seconds)


def stamp(offset_s: float = 0.0) -> str:
    t = datetime.now(UTC) + timedelta(seconds=offset_s)
    return t.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def parse(iso: str) -> datetime:
    assert ISO_Z.fullmatch(iso), iso
    return datetime.fromisoformat(iso.replace("Z", "+00:00"))


def traffic(world: World) -> list[dict]:
    """An aircraft over the D crossing, a truck on it and a person off the airfield."""
    lon, lat = world.points["D_CROSSING"]
    return [
        {"object_id": "AC8646", "kind": "aircraft", "callsign": "AC8646", "label": "A320",
         "status": "airborne", "lat": lat, "lon": lon, "alt_ft": 1200, "heading_deg": 44,
         "speed_kt": 135},
        {"object_id": "TRUCK1", "kind": "vehicle", "callsign": "TRUCK1", "label": "Fire truck",
         "status": "ground", "lat": lat, "lon": lon, "heading_deg": 314, "speed_kt": 0},
        {"object_id": "OBJ-17", "kind": "person", "callsign": None, "label": "Person on foot",
         "status": "ground", "lat": 40.70, "lon": -73.80},
    ]


def positions(client, world, **change) -> httpx.Response:
    body = {"tower_id": "LGA", "t": stamp(), "objects": traffic(world), **change}
    return client.post("/ingest/positions", json=body)


def say(client, stream_id: str, speaker: str | None, text: str, **change) -> httpx.Response:
    """Post an utterance as a transcript."""
    body = {"tower_id": "LGA", "stream_id": stream_id, "t_start": stamp(-3), "duration_s": 3.0,
            "speaker": speaker, "text": text, **change}
    return client.post("/ingest/transcripts", json=body)


def send_audio(client, change: dict | None = None, *, data: bytes = WAV,
               mime: str = "audio/wav") -> httpx.Response:
    """Post an utterance as audio; by default the tower's landing clearance."""
    meta = {"tower_id": "LGA", "stream_id": "LGA_TWR", "t_start": stamp(-5), "duration_s": 4.6,
            "speaker": "TOWER", "truth_text": LANDING, **(change or {})}
    return client.post("/ingest/audio", data={"meta": json.dumps(meta)},
                       files={"file": ("clip.wav", data, mime)})


def outbound(client, since: str | None = None) -> dict:
    params = {"tower_id": "LGA"} | ({"since": since} if since else {})
    r = client.get("/ingest/outbound", params=params)
    assert r.status_code == 200, r.text
    return r.json()


def assert_error(r: httpx.Response, status: int, field: str) -> None:
    assert r.status_code == status, r.text
    assert r.json()["field"] == field
    assert field in r.json()["detail"]


# --------------------------------------------------------------------------- world
def test_world_is_the_bundled_world_with_lane_areas(client, world):
    r = client.get("/ingest/world", params={"tower_id": "LGA"})
    assert r.status_code == 200
    body = r.json()
    assert body == world.as_ingest_world()
    assert set(body) == {"tower", "lanes", "hold_points", "controllers", "streams", "points"}
    assert all(lane["area"]["type"] == "Polygon" for lane in body["lanes"])
    assert [s["_id"] for s in body["streams"]] == ["LGA_TWR", "LGA_GND", "LGA_OPS"]


def test_world_errors(client):
    assert_error(client.get("/ingest/world", params={"tower_id": "JFK"}), 404, "tower_id")
    assert_error(client.get("/ingest/world"), 400, "tower_id")


# ----------------------------------------------------------------------- positions
def test_positions_are_accepted_and_each_object_gets_its_lane(client, store, world):
    sent = traffic(world)
    r = positions(client, world)
    assert r.status_code == 200
    assert r.json() == {"accepted": 3,
                        "lanes": {"AC8646": None, "TRUCK1": "RWY_04_22", "OBJ-17": None}}

    assert set(store.objects) == {"AC8646", "TRUCK1", "OBJ-17"}
    truck = store.objects["TRUCK1"]
    assert truck == {**sent[1], "lane_id": "RWY_04_22", "t": truck["t"]}
    assert store.objects["AC8646"]["lane_id"] is None
    [batch] = store.position_batches
    assert batch["objects"] == sent and batch["tower_id"] == "LGA"
    parse(batch["received_at"])


def test_positions_lane_follows_the_object(client, store, world):
    lon, lat = world.hold_points["HP_D_SE"]
    moved = [{"object_id": "TRUCK1", "kind": "vehicle", "status": "ground", "lat": lat, "lon": lon}]
    positions(client, world)
    r = positions(client, world, objects=moved)
    assert r.json() == {"accepted": 1, "lanes": {"TRUCK1": "TWY_D"}}
    assert store.objects["TRUCK1"]["lane_id"] == "TWY_D"
    assert "callsign" not in store.objects["TRUCK1"]  # the last report, not a merge
    assert len(store.objects) == 3 and len(store.position_batches) == 2


def test_positions_t_defaults_to_now(client, store, world):
    r = client.post("/ingest/positions", json={"tower_id": "LGA", "objects": traffic(world)})
    assert r.status_code == 200
    age = datetime.now(UTC) - parse(store.objects["TRUCK1"]["t"])
    assert timedelta(0) <= age < timedelta(seconds=5)
    assert store.position_batches[0]["t"] == store.objects["TRUCK1"]["t"]  # filled in there too


def test_positions_takes_500_objects_but_not_501(client, store):
    def crowd(n):
        return [{"object_id": f"OBJ-{i}", "kind": "unknown", "status": "ground",
                 "lat": 40.77, "lon": -73.87} for i in range(n)]

    assert_error(client.post("/ingest/positions", json={"tower_id": "LGA", "objects": crowd(501)}),
                 400, "objects")
    assert store.objects == {}
    r = client.post("/ingest/positions", json={"tower_id": "LGA", "objects": crowd(500)})
    assert r.status_code == 200 and r.json()["accepted"] == 500


@pytest.mark.parametrize("name", ["object_id", "kind", "status", "lat", "lon"])
def test_positions_missing_object_field_is_400(client, store, world, name):
    objects = traffic(world)
    del objects[1][name]
    assert_error(positions(client, world, objects=objects), 400, f"objects[1].{name}")
    assert store.objects == {} and store.position_batches == []  # nothing of a bad call is kept


@pytest.mark.parametrize("name, value", [
    ("object_id", ""), ("kind", "boat"), ("status", "taxiing"), ("lat", "north"), ("lat", 91),
    ("lon", -181), ("lon", True), ("alt_ft", "high"), ("speed_kt", [1]), ("callsign", 8646),
])
def test_positions_bad_object_field_is_400(client, world, name, value):
    objects = traffic(world)
    objects[2][name] = value
    assert_error(positions(client, world, objects=objects), 400, f"objects[2].{name}")


def test_positions_bad_body_is_400(client, world):
    not_json = client.post("/ingest/positions", content=b"{nope",
                           headers={"content-type": "application/json"})
    assert_error(not_json, 400, "body")
    assert_error(client.post("/ingest/positions", json=[1, 2]), 400, "body")
    assert_error(client.post("/ingest/positions", json={"objects": []}), 400, "tower_id")
    assert_error(client.post("/ingest/positions", json={"tower_id": "LGA"}), 400, "objects")
    assert_error(positions(client, world, objects=["AC8646"]), 400, "objects[0]")
    assert_error(positions(client, world, t="yesterday"), 400, "t")
    assert_error(positions(client, world, t="2026-11-14T19:05:12"), 400, "t")  # no Z: not UTC


def test_positions_unknown_tower_is_404(client, world):
    assert_error(positions(client, world, tower_id="JFK"), 404, "tower_id")


@pytest.mark.parametrize("offset_s, status", [(-300, 422), (300, 422), (-100, 200), (100, 200)])
def test_positions_t_must_be_within_120s_of_the_clock(client, store, world, offset_s, status):
    r = positions(client, world, t=stamp(offset_s))
    assert r.status_code == status
    if status == 422:
        assert_error(r, 422, "t")
        assert store.objects == {}


def test_object_not_reported_for_120s_disappears(client, store, world):
    clock = store.now = Clock()
    assert client.post("/ingest/positions",
                       json={"tower_id": "LGA", "objects": traffic(world)}).status_code == 200
    clock.advance(100)
    client.post("/ingest/positions", json={"tower_id": "LGA", "objects": traffic(world)[1:2]})
    clock.advance(30)
    assert set(store.objects) == {"TRUCK1"}
    gone = client.post("/ingest/objects/AC8646/remove", json={"tower_id": "LGA"})
    assert_error(gone, 404, "object_id")


# -------------------------------------------------------------------------- remove
def test_remove_object(client, store, world):
    positions(client, world)
    r = client.post("/ingest/objects/TRUCK1/remove",
                    json={"tower_id": "LGA", "reason": "parked at gate"})
    assert r.status_code == 200 and r.json() == {"removed": True}
    assert set(store.objects) == {"AC8646", "OBJ-17"}
    [record] = store.removed
    assert record["object_id"] == "TRUCK1" and record["reason"] == "parked at gate"
    parse(record["received_at"])


def test_remove_errors(client, store, world):
    positions(client, world)
    assert_error(client.post("/ingest/objects/NOBODY/remove", json={"tower_id": "LGA"}),
                 404, "object_id")
    assert_error(client.post("/ingest/objects/TRUCK1/remove", json={"tower_id": "JFK"}),
                 404, "tower_id")
    assert_error(client.post("/ingest/objects/TRUCK1/remove", json={}), 400, "tower_id")
    assert_error(client.post("/ingest/objects/TRUCK1/remove",
                             json={"tower_id": "LGA", "reason": 5}), 400, "reason")
    assert len(store.objects) == 3 and store.removed == []


# --------------------------------------------------------------------------- audio
def test_audio_is_accepted_and_recorded_without_its_bytes(client, store):
    r = send_audio(client)
    assert r.status_code == 202
    [clip] = store.clips
    assert r.json() == {"clip_id": clip["clip_id"]}
    assert set(clip) == CLIP_KEYS
    assert clip | {"clip_id": "", "t_start": "", "received_at": ""} == {
        "clip_id": "", "kind": "audio", "stream_id": "LGA_TWR", "t_start": "", "duration_s": 4.6,
        "speaker": "TOWER", "text": LANDING, "size_bytes": len(WAV), "mime": "audio/wav",
        "received_at": ""}
    assert parse(clip["received_at"]) - parse(clip["t_start"]) < timedelta(seconds=10)


def test_audio_speaker_and_truth_text_are_optional(client, store):
    meta = {"tower_id": "LGA", "stream_id": "LGA_GND", "t_start": stamp(-2), "duration_s": 2}
    r = client.post("/ingest/audio", data={"meta": json.dumps(meta)},
                    files={"file": ("clip.mp3", b"ID3" + bytes(64), "audio/mpeg")})
    assert r.status_code == 202
    assert store.clips[0]["speaker"] is None and store.clips[0]["text"] is None
    assert store.clips[0]["size_bytes"] == 67 and store.clips[0]["mime"] == "audio/mpeg"


@pytest.mark.parametrize("mime", ["audio/wav", "audio/mpeg", "audio/ogg", "audio/ogg; codecs=opus"])
def test_audio_types_in_the_contract_are_accepted(client, mime):
    assert send_audio(client, mime=mime).status_code == 202


def test_audio_burst_on_three_streams_is_all_recorded(client, store):
    said = {"LGA_TWR": ("TOWER", LANDING), "LGA_GND": ("GROUND", CROSSING),
            "LGA_OPS": ("OPS", "Ops one, proceed to the fire station.")}
    together = threading.Barrier(len(said))

    def post(stream_id: str) -> httpx.Response:
        speaker, text = said[stream_id]
        together.wait(timeout=5)
        return send_audio(client, {"stream_id": stream_id, "speaker": speaker, "truth_text": text})

    with ThreadPoolExecutor(len(said)) as pool:
        answers = list(pool.map(post, said))

    assert [r.status_code for r in answers] == [202, 202, 202]
    assert len(store.clips) == 3
    assert {c["clip_id"] for c in store.clips} == {r.json()["clip_id"] for r in answers}
    assert {c["stream_id"]: (c["speaker"], c["text"]) for c in store.clips} == said


def test_audio_bad_parts_are_400(client, store):
    meta = {"tower_id": "LGA", "stream_id": "LGA_TWR", "t_start": stamp(-5), "duration_s": 4.6}
    file = {"file": ("clip.wav", WAV, "audio/wav")}
    post = client.post
    assert_error(post("/ingest/audio", files=file), 400, "meta")
    assert_error(post("/ingest/audio", data={"meta": "{nope"}, files=file), 400, "meta")
    assert_error(post("/ingest/audio", data={"meta": '"LGA"'}, files=file), 400, "meta")
    assert_error(post("/ingest/audio", data={"meta": json.dumps(meta)}), 400, "file")
    assert_error(post("/ingest/audio", json=meta), 400, "meta")  # not a multipart form at all
    assert_error(send_audio(client, mime="text/plain"), 400, "file")
    assert_error(send_audio(client, data=b""), 400, "file")
    for name in ("tower_id", "stream_id", "t_start", "duration_s"):
        without = {k: v for k, v in meta.items() if k != name}
        r = post("/ingest/audio", data={"meta": json.dumps(without)}, files=file)
        assert_error(r, 400, f"meta.{name}")
    assert_error(send_audio(client, {"duration_s": "long"}), 400, "meta.duration_s")
    assert_error(send_audio(client, {"t_start": "just now"}), 400, "meta.t_start")
    assert_error(send_audio(client, {"truth_text": 7}), 400, "meta.truth_text")
    assert store.clips == []


def test_audio_unknown_ids_are_404(client, store):
    assert_error(send_audio(client, {"stream_id": "LGA_APP"}), 404, "meta.stream_id")
    assert_error(send_audio(client, {"tower_id": "JFK"}), 404, "meta.tower_id")
    assert store.clips == []


def test_audio_over_5mb_or_60s_is_413(client, store):
    assert_error(send_audio(client, data=bytes(5 * MB + 1)), 413, "file")
    assert_error(send_audio(client, {"duration_s": 60.5}), 413, "meta.duration_s")
    assert store.clips == []
    assert send_audio(client, {"duration_s": 60}, data=bytes(5 * MB)).status_code == 202
    assert store.clips[0]["size_bytes"] == 5 * MB


@pytest.mark.parametrize("offset_s", [-300, 300])
def test_audio_t_start_far_from_the_clock_is_422(client, store, offset_s):
    assert_error(send_audio(client, {"t_start": stamp(offset_s)}), 422, "meta.t_start")
    assert store.clips == []


# --------------------------------------------------------------------- transcripts
def test_transcript_is_accepted_and_recorded(client, store):
    r = say(client, "LGA_GND", "GROUND", CROSSING, confidence=0.93)
    assert r.status_code == 202
    [clip] = store.clips
    assert r.json() == {"clip_id": clip["clip_id"]}
    assert set(clip) == CLIP_KEYS
    assert (clip["kind"], clip["stream_id"], clip["speaker"], clip["text"], clip["duration_s"]) == (
        "transcript", "LGA_GND", "GROUND", CROSSING, 3.0)
    assert clip["size_bytes"] is None and clip["mime"] is None
    parse(clip["t_start"]), parse(clip["received_at"])


def test_transcript_optional_fields_can_be_left_out(client, store):
    r = client.post("/ingest/transcripts", json={"tower_id": "LGA", "stream_id": "LGA_OPS",
                                                 "t_start": stamp(), "text": "Radio check."})
    assert r.status_code == 202
    assert store.clips[0]["duration_s"] is None and store.clips[0]["speaker"] is None


def test_transcript_errors(client, store):
    assert_error(say(client, "LGA_GND", "GROUND", None), 400, "text")
    assert_error(say(client, "LGA_GND", "GROUND", "  "), 400, "text")
    assert_error(say(client, None, "GROUND", CROSSING), 400, "stream_id")
    assert_error(say(client, "LGA_GND", "GROUND", CROSSING, t_start=None), 400, "t_start")
    assert_error(say(client, "LGA_GND", "GROUND", CROSSING, duration_s=-1), 400, "duration_s")
    assert_error(say(client, "LGA_GND", "GROUND", CROSSING, confidence=1.5), 400, "confidence")
    assert_error(say(client, "LGA_GND", ["GROUND"], CROSSING), 400, "speaker")
    assert_error(say(client, "LGA_APP", "APPROACH", CROSSING), 404, "stream_id")
    assert_error(say(client, "LGA_GND", "GROUND", CROSSING, tower_id="JFK"), 404, "tower_id")
    assert_error(say(client, "LGA_GND", "GROUND", CROSSING, duration_s=61), 413, "duration_s")
    assert_error(say(client, "LGA_GND", "GROUND", CROSSING, t_start=stamp(300)), 422, "t_start")
    assert_error(say(client, "LGA_GND", "GROUND", CROSSING, t_start=stamp(-300)), 422, "t_start")
    assert store.clips == []


# ------------------------------------------------------------------------- weather
def test_weather_is_recorded(client, store):
    r = client.post("/ingest/weather", json={"tower_id": "LGA", "t": stamp(), "visibility_sm": 0.75,
                                             "wind": "03012KT", "raw": "KLGA 141851Z 03012KT 3/4SM"})
    assert r.status_code == 200
    assert client.post("/ingest/weather", json={"tower_id": "LGA", "visibility_sm": 10,
                                                "wind": "00000KT"}).status_code == 200
    low, clear = store.weather
    assert (low["visibility_sm"], low["wind"], low["raw"]) == (0.75, "03012KT",
                                                               "KLGA 141851Z 03012KT 3/4SM")
    assert clear["visibility_sm"] == 10 and "raw" not in clear
    assert datetime.now(UTC) - parse(clear["t"]) < timedelta(seconds=5)  # t defaulted
    parse(low["t"]), parse(low["received_at"])


def test_weather_errors(client, store):
    ok = {"tower_id": "LGA", "visibility_sm": 0.75, "wind": "03012KT"}
    post = client.post
    assert_error(post("/ingest/weather", json={**ok, "visibility_sm": None}), 400, "visibility_sm")
    assert_error(post("/ingest/weather", json={**ok, "visibility_sm": -1}), 400, "visibility_sm")
    assert_error(post("/ingest/weather", json={**ok, "wind": None}), 400, "wind")
    assert_error(post("/ingest/weather", json={**ok, "t": "noon"}), 400, "t")
    assert_error(post("/ingest/weather", json={**ok, "tower_id": "JFK"}), 404, "tower_id")
    assert_error(post("/ingest/weather", json={**ok, "t": stamp(-300)}), 422, "t")
    assert store.weather == []


# ------------------------------------------------------------------------- streams
def test_streams_start_as_the_three_in_the_world(store):
    assert list(store.streams) == ["LGA_TWR", "LGA_GND", "LGA_OPS"]


def test_stream_can_be_created_updated_and_then_used(client, store):
    approach = {"id": "LGA_APP", "tower_id": "LGA", "name": "Approach 120.8", "kind": "approach",
                "frequency_mhz": 120.8, "controller_id": "ctl_local", "station": "APPROACH"}
    assert_error(say(client, "LGA_APP", "APPROACH", "Radio check."), 404, "stream_id")
    assert client.post("/ingest/streams", json=approach).status_code == 200
    assert store.streams["LGA_APP"] == {
        "_id": "LGA_APP", "name": "Approach 120.8", "kind": "approach", "frequency_mhz": 120.8,
        "controller_id": "ctl_local", "station": "APPROACH"}
    assert say(client, "LGA_APP", "APPROACH", "Radio check.").status_code == 202

    renamed = {"id": "LGA_APP", "tower_id": "LGA", "name": "Approach 124.95", "frequency_mhz": 124.95}
    assert client.post("/ingest/streams", json=renamed).status_code == 200
    assert store.streams["LGA_APP"]["name"] == "Approach 124.95"
    assert store.streams["LGA_APP"]["station"] == "APPROACH"  # an update keeps what it does not name
    listed = client.get("/ingest/world", params={"tower_id": "LGA"}).json()["streams"]
    assert [s["_id"] for s in listed] == ["LGA_TWR", "LGA_GND", "LGA_OPS", "LGA_APP"]


def test_stream_errors(client, store):
    assert_error(client.post("/ingest/streams", json={"tower_id": "LGA", "name": "x"}), 400, "id")
    assert_error(client.post("/ingest/streams", json={"id": "LGA_APP", "tower_id": "LGA",
                                                      "frequency_mhz": "120.8"}), 400, "frequency_mhz")
    assert_error(client.post("/ingest/streams", json={"id": "LGA_APP"}), 400, "tower_id")
    assert_error(client.post("/ingest/streams", json={"id": "JFK_TWR", "tower_id": "JFK"}),
                 404, "tower_id")
    assert list(store.streams) == ["LGA_TWR", "LGA_GND", "LGA_OPS"]


# ---------------------------------------------------------------------------- runs
def test_run_is_started_and_ended(client, store):
    r = client.post("/ingest/runs", json={"tower_id": "LGA", "scenario": "laguardia_crossing",
                                          "kind": "incident", "note": "truck crosses at D"})
    assert r.status_code == 200
    run_id = r.json()["run_id"]
    [run] = store.runs
    assert run == {"run_id": run_id, "scenario": "laguardia_crossing", "kind": "incident",
                   "note": "truck crosses at D", "started_at": run["started_at"], "ended_at": None}
    parse(run["started_at"])

    assert client.post(f"/ingest/runs/{run_id}/end").status_code == 200  # no body needed
    ended_at = store.runs[0]["ended_at"]
    assert parse(ended_at) >= parse(run["started_at"])
    assert client.post(f"/ingest/runs/{run_id}/end", json={"tower_id": "LGA"}).status_code == 200
    assert store.runs[0]["ended_at"] == ended_at  # ending twice changes nothing


def test_run_errors(client, store):
    ok = {"tower_id": "LGA", "scenario": "normal_traffic", "kind": "normal"}
    assert_error(client.post("/ingest/runs", json={**ok, "scenario": ""}), 400, "scenario")
    assert_error(client.post("/ingest/runs", json={**ok, "kind": "drill"}), 400, "kind")
    assert_error(client.post("/ingest/runs", json={**ok, "note": 3}), 400, "note")
    assert_error(client.post("/ingest/runs", json={**ok, "tower_id": "JFK"}), 404, "tower_id")
    assert store.runs == []
    assert_error(client.post("/ingest/runs/nope/end", json={"tower_id": "LGA"}), 404, "run_id")
    run_id = client.post("/ingest/runs", json=ok).json()["run_id"]
    assert_error(client.post(f"/ingest/runs/{run_id}/end", json={"tower_id": "JFK"}),
                 404, "tower_id")
    assert store.runs[0]["ended_at"] is None


# --------------------------------------------------------------------------- reset
def test_reset_clears_objects_only(client, store, world):
    positions(client, world)
    send_audio(client)
    say(client, "LGA_GND", "GROUND", CROSSING)
    client.post("/ingest/weather", json={"tower_id": "LGA", "visibility_sm": 3, "wind": "03012KT"})
    client.post("/ingest/runs", json={"tower_id": "LGA", "scenario": "s", "kind": "normal"})
    client.post("/ingest/objects/OBJ-17/remove", json={"tower_id": "LGA"})
    store.add_outbound(target=["TRUCK1"], text=STOP_TEXT)
    assert len(store.objects) == 2

    r = client.post("/ingest/reset", json={"tower_id": "LGA", "scope": "live"})
    assert r.status_code == 200
    assert store.objects == {}
    kept = (store.position_batches, store.clips, store.weather, store.runs, store.removed,
            store.outbound, store.streams)
    assert [len(x) for x in kept] == [1, 2, 1, 1, 1, 1, 3]
    [record] = store.resets
    assert record["scope"] == "live" and record["tower_id"] == "LGA"
    parse(record["received_at"])
    assert len(outbound(client)["calls"]) == 1

    assert positions(client, world).json()["accepted"] == 3  # and it takes traffic again
    assert len(store.objects) == 3


def test_reset_errors(client, store, world):
    positions(client, world)
    assert_error(client.post("/ingest/reset", json={"tower_id": "LGA", "scope": "all"}), 400, "scope")
    assert_error(client.post("/ingest/reset", json={"tower_id": "LGA"}), 400, "scope")
    assert_error(client.post("/ingest/reset", json={"tower_id": "JFK", "scope": "live"}),
                 404, "tower_id")
    assert len(store.objects) == 3 and store.resets == []


# ------------------------------------------------------------------------ outbound
def test_outbound_is_empty_until_the_stub_says_something(client):
    body = outbound(client)
    assert body["calls"] == []
    assert datetime.now(UTC) - parse(body["now"]) < timedelta(seconds=5)


def test_outbound_call_has_the_contract_shape(client, store):
    made = store.add_outbound(target=["TRUCK1"], text=STOP_TEXT, lane_id="RWY_04_22",
                              stream_id="LGA_GND", approved_by="ctl_local")
    [call] = outbound(client)["calls"]
    assert call == made
    assert set(call) == CALL_KEYS
    assert call | {"t": "", "decision_id": "", "clip_id": ""} == {
        "t": "", "decision_id": "", "stream_id": "LGA_GND", "action": "stop", "target": ["TRUCK1"],
        "lane_id": "RWY_04_22", "text": STOP_TEXT, "approved_by": "ctl_local", "clip_id": ""}
    parse(call["t"])
    assert call["decision_id"] and call["clip_id"]

    plain = store.add_outbound(action="propose_instruction", target=["AC8646"], text="Go around.")
    assert (plain["lane_id"], plain["stream_id"], plain["approved_by"]) == (None, None, "stub")


def test_outbound_since_returns_only_later_calls(client, store):
    first = store.add_outbound(target=["TRUCK1"], text="one")
    poll = outbound(client)
    assert poll["calls"] == [first]

    second = store.add_outbound(target=["TRUCK1"], text="two")
    assert outbound(client, since=poll["now"])["calls"] == [second]
    assert outbound(client, since=first["t"])["calls"] == [second]
    assert outbound(client)["calls"] == [first, second]  # no since: everything
    assert outbound(client, since=stamp(60))["calls"] == []


def test_outbound_polled_with_the_returned_now_gives_each_call_once(client, store):
    seen, since = [], None
    for i in range(20):  # calls and polls land in the same millisecond here
        store.add_outbound(target=["TRUCK1"], text=f"call {i}")
        if i % 3 == 0:
            store.add_outbound(target=["TRUCK1"], text=f"call {i} again")
        poll = outbound(client, since)
        seen += [c["text"] for c in poll["calls"]]
        since = poll["now"]
    assert seen == [c["text"] for c in store.outbound] and len(seen) == 27
    assert outbound(client, since)["calls"] == []


def test_outbound_errors(client):
    assert_error(client.get("/ingest/outbound"), 400, "tower_id")
    assert_error(client.get("/ingest/outbound", params={"tower_id": "JFK"}), 404, "tower_id")
    assert_error(client.get("/ingest/outbound", params={"tower_id": "LGA", "since": "earlier"}),
                 400, "since")


# ---------------------------------------------------------------------- ingest key
INGEST_CALLS = [
    ("GET", "/ingest/world"), ("POST", "/ingest/positions"), ("POST", "/ingest/objects/X/remove"),
    ("POST", "/ingest/audio"), ("POST", "/ingest/transcripts"), ("POST", "/ingest/weather"),
    ("POST", "/ingest/streams"), ("POST", "/ingest/runs"), ("POST", "/ingest/runs/X/end"),
    ("POST", "/ingest/reset"), ("GET", "/ingest/outbound"),
]


@pytest.mark.parametrize("method, path", INGEST_CALLS)
def test_missing_or_wrong_ingest_key_is_401(world, method, path):
    client = TestClient(create_stub_app(world, ingest_key="test-key"))
    assert_error(client.request(method, path), 401, "X-Ingest-Key")
    assert_error(client.request(method, path, headers={"X-Ingest-Key": "wrong"}), 401, "X-Ingest-Key")


def test_right_ingest_key_is_accepted_and_helpers_need_none(world):
    app = create_stub_app(world, ingest_key="test-key")
    keyed = TestClient(app, headers={"X-Ingest-Key": "test-key"})
    assert keyed.get("/ingest/world", params={"tower_id": "LGA"}).status_code == 200
    assert positions(keyed, world).status_code == 200
    assert send_audio(keyed).status_code == 202
    assert len(app.state.store.objects) == 3 and len(app.state.store.clips) == 1
    assert TestClient(app).get("/health").status_code == 200
    assert TestClient(app).get("/stub/state").status_code == 200


def test_no_ingest_key_means_none_is_checked(client, world):
    r = client.post("/ingest/positions", headers={"X-Ingest-Key": "anything"},
                    json={"tower_id": "LGA", "objects": traffic(world)})
    assert r.status_code == 200


# ------------------------------------------------------------- the auto-stop rule
@pytest.fixture
def armed(world):
    """A stub whose rule stops a crossing 5 s after it is cleared: (client, store, clock)."""
    app = create_stub_app(world, auto_stop_after_s=5.0)
    clock = app.state.store.now = Clock()
    client = TestClient(app)
    assert positions(client, world).status_code == 200  # callsigns come from the objects reported
    return client, app.state.store, clock


def test_auto_stop_appears_after_the_delay(armed):
    client, store, clock = armed
    assert say(client, "LGA_TWR", "TOWER", LANDING).status_code == 202
    assert say(client, "LGA_GND", "GROUND", CROSSING).status_code == 202
    first = outbound(client)
    assert first["calls"] == []
    clock.advance(4.9)
    assert outbound(client)["calls"] == []

    clock.advance(0.2)
    [call] = outbound(client, since=first["now"])["calls"]
    assert set(call) == CALL_KEYS
    assert (call["action"], call["target"], call["lane_id"], call["stream_id"], call["text"],
            call["approved_by"]) == ("stop", ["TRUCK1"], "RWY_04_22", "LGA_GND", STOP_TEXT, "stub")
    assert parse(call["t"]) - parse(store.clips[-1]["received_at"]) == timedelta(seconds=5)

    clock.advance(10)
    assert outbound(client)["calls"] == [call]  # once, however long it is polled
    assert store.outbound == [call]


def test_auto_stop_hears_audio_and_shows_in_the_store_without_a_poll(armed):
    client, store, clock = armed
    assert send_audio(client).status_code == 202  # the landing clearance, as audio on Tower
    assert send_audio(client, {"stream_id": "LGA_GND", "speaker": "GROUND",
                               "truth_text": CROSSING}).status_code == 202
    assert store.outbound == []
    clock.advance(5.1)
    assert [c["target"] for c in store.outbound] == [["TRUCK1"]]


def test_auto_stop_with_the_real_clock(world):
    app = create_stub_app(world, auto_stop_after_s=0.4)
    client = TestClient(app)
    positions(client, world)
    say(client, "LGA_TWR", "TOWER", LANDING)
    say(client, "LGA_GND", "GROUND", CROSSING)
    assert outbound(client)["calls"] == []

    deadline = time.monotonic() + 5
    while not (calls := outbound(client)["calls"]) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert [c["target"] for c in calls] == [["TRUCK1"]]
    cleared_at = parse(app.state.store.clips[-1]["received_at"])
    assert parse(calls[0]["t"]) - cleared_at == timedelta(seconds=0.4)


@pytest.mark.parametrize("speaker, report", [
    ("AC8646", "Clear of runway four at Bravo, Air Canada eight six four six."),
    ("AC8646", "Runway vacated."),
    ("PILOT", "Air Canada eight six four six, clear of runway four."),
])
def test_no_auto_stop_once_the_landing_aircraft_is_clear_of_the_runway(armed, speaker, report):
    client, store, clock = armed
    say(client, "LGA_TWR", "TOWER", LANDING)
    say(client, "LGA_TWR", speaker, report)
    say(client, "LGA_GND", "GROUND", CROSSING)
    clock.advance(10)
    assert outbound(client)["calls"] == [] and store.outbound == []


def test_only_the_landing_aircraft_can_report_itself_clear(armed):
    client, store, clock = armed
    say(client, "LGA_TWR", "TOWER", LANDING)
    say(client, "LGA_GND", "TRUCK1", "Truck one will cross once Air Canada eight six four six "
                                     "is clear of runway four.")
    say(client, "LGA_GND", "GROUND", CROSSING)
    clock.advance(10)
    assert [c["target"] for c in store.outbound] == [["TRUCK1"]]


def test_no_auto_stop_when_the_rule_is_off(world):
    app = create_stub_app(world)  # auto_stop_after_s is None
    clock = app.state.store.now = Clock()
    client = TestClient(app)
    positions(client, world)
    say(client, "LGA_TWR", "TOWER", LANDING)
    say(client, "LGA_GND", "GROUND", CROSSING)
    assert outbound(client)["calls"] == []
    clock.advance(110)
    assert outbound(client)["calls"] == [] and app.state.store.outbound == []


def test_no_auto_stop_without_a_landing_clearance(armed):
    client, store, clock = armed
    say(client, "LGA_GND", "GROUND", CROSSING)
    say(client, "LGA_GND", "TRUCK1", "Cross runway four at Delta, Truck one.")
    clock.advance(10)
    assert store.outbound == []


def test_auto_stop_ignores_what_traffic_says(armed):
    client, store, clock = armed
    # read back by the pilot only: no controller cleared anyone to land
    say(client, "LGA_TWR", "AC8646", "Runway four, cleared to land, Air Canada eight six four six.")
    say(client, "LGA_GND", "GROUND", CROSSING)
    clock.advance(10)
    assert store.outbound == []

    say(client, "LGA_TWR", "TOWER", LANDING)
    say(client, "LGA_GND", "GROUND", CROSSING)
    say(client, "LGA_GND", "TRUCK1", "Cross runway four at Delta, Truck one.")  # the readback
    clock.advance(10)
    assert len(store.outbound) == 1


def test_auto_stop_needs_callsigns_of_reported_objects(world):
    app = create_stub_app(world, auto_stop_after_s=0.0)
    client = TestClient(app)  # no positions posted: the stub knows no callsigns
    say(client, "LGA_TWR", "TOWER", LANDING)
    say(client, "LGA_GND", "GROUND", CROSSING)
    assert outbound(client)["calls"] == []


def test_auto_stop_is_about_the_runway_being_landed_on(armed):
    client, store, clock = armed
    say(client, "LGA_TWR", "TOWER", LANDING)
    say(client, "LGA_GND", "GROUND", "Truck one, cross runway one three at Bravo.")
    clock.advance(10)
    assert store.outbound == []

    say(client, "LGA_TWR", "TOWER", "Air Canada eight six four six, cleared to land runway one three.")
    say(client, "LGA_OPS", "OPS", "Truck one, cross runway one three at Bravo.")
    clock.advance(10)
    [call] = store.outbound
    assert (call["lane_id"], call["stream_id"]) == ("RWY_13_31", "LGA_OPS")
    assert call["text"] == "Truck one, STOP. Hold short runway one three."


@pytest.mark.parametrize("landing", [
    "Air Canada eight six four six, runway zero four, cleared to land.",
    "Air Canada eight six four six, cleared to land runway four, zero three zero at one two.",
    "Air Canada eight six four six, wind 030 at 12, runway 04, cleared to land.",
])
def test_auto_stop_understands_how_runways_are_said(armed, landing):
    client, store, clock = armed
    say(client, "LGA_TWR", "TOWER", landing)
    say(client, "LGA_GND", "GROUND", "Truck one, cross runway two two at Delta.")  # 04's other end
    clock.advance(10)
    [call] = store.outbound
    assert call["lane_id"] == "RWY_04_22"
    assert call["text"] == "Truck one, STOP. Hold short runway two two."


def test_reset_ends_the_landing_clearance_and_cancels_a_pending_stop(armed, world):
    client, store, clock = armed
    say(client, "LGA_TWR", "TOWER", LANDING)
    say(client, "LGA_GND", "GROUND", CROSSING)
    clock.advance(2)  # the stop is due in 3 s
    assert client.post("/ingest/reset", json={"tower_id": "LGA", "scope": "live"}).status_code == 200
    clock.advance(10)
    assert store.outbound == []  # the pending stop went with the reset

    positions(client, world)
    assert say(client, "LGA_GND", "GROUND", CROSSING).status_code == 202
    clock.advance(10)
    assert store.outbound == []  # and so did the landing clearance


# --------------------------------------------------------------- developer helpers
def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200 and r.json() == {"status": "ok", "stub": True}


def test_cors_allows_any_origin(client):
    r = client.get("/health", headers={"Origin": "http://localhost:5173"})
    assert r.headers["access-control-allow-origin"] == "*"
    preflight = client.options("/ingest/positions", headers={
        "Origin": "http://localhost:5173", "Access-Control-Request-Method": "POST",
        "Access-Control-Request-Headers": "content-type,x-ingest-key"})
    assert preflight.status_code == 200
    assert preflight.headers["access-control-allow-origin"] == "*"


def test_stub_state_counts_everything_and_shows_the_last_20_clips(client, store, world):
    positions(client, world)
    for i in range(24):
        say(client, "LGA_GND", "GROUND", f"Radio check {i}.")
    send_audio(client)
    client.post("/ingest/weather", json={"tower_id": "LGA", "visibility_sm": 3, "wind": "03012KT"})
    client.post("/ingest/runs", json={"tower_id": "LGA", "scenario": "s", "kind": "normal"})
    client.post("/ingest/objects/OBJ-17/remove", json={"tower_id": "LGA"})
    call = store.add_outbound(target=["TRUCK1"], text=STOP_TEXT)

    r = client.get("/stub/state")
    assert r.status_code == 200
    state = r.json()
    assert state["counts"] == {"objects": 2, "position_batches": 1, "clips": 25, "weather": 1,
                               "runs": 1, "resets": 0, "removed": 1, "streams": 3, "outbound": 1}
    assert state["clips"] == store.clips[-20:] and len(state["clips"]) == 20
    assert state["clips"][-1]["kind"] == "audio" and state["clips"][-1]["size_bytes"] == len(WAV)
    assert set(state["objects"]) == {"AC8646", "TRUCK1"}
    assert state["objects"]["TRUCK1"]["lane_id"] == "RWY_04_22"
    assert state["outbound"] == [call]
    parse(state["now"])


def test_stub_outbound_injects_a_call(client, store):
    r = client.post("/stub/outbound", json={"action": "stop", "target": ["TRUCK1"],
                                            "text": STOP_TEXT, "lane_id": "RWY_04_22",
                                            "stream_id": "LGA_GND"})
    assert r.status_code == 200
    assert set(r.json()) == CALL_KEYS
    assert outbound(client)["calls"] == [r.json()] == store.outbound

    r = client.post("/stub/outbound", json={"action": "propose_instruction", "target": ["AC8646"],
                                            "text": "Air Canada eight six four six, go around."})
    assert r.json()["action"] == "propose_instruction" and r.json()["lane_id"] is None

    # action defaults to "stop"; an empty target is a call to nobody in particular
    r = client.post("/stub/outbound", json={"target": [], "lane_id": "RWY_04_22",
                                            "text": "Vehicle on runway zero four, STOP."})
    assert r.status_code == 200 and (r.json()["action"], r.json()["target"]) == ("stop", [])
    assert len(store.outbound) == 3


def test_stub_outbound_errors(client, store):
    ok = {"action": "stop", "target": ["TRUCK1"], "text": STOP_TEXT}
    assert_error(client.post("/stub/outbound", json={**ok, "text": None}), 400, "text")
    assert_error(client.post("/stub/outbound", json={**ok, "target": "TRUCK1"}), 400, "target")
    assert_error(client.post("/stub/outbound", json={**ok, "target": [7]}), 400, "target")
    assert_error(client.post("/stub/outbound", json={**ok, "action": "warn"}), 400, "action")
    assert store.outbound == []


# ------------------------------------------------------------------- under uvicorn
def test_closed_loop_over_real_http_under_uvicorn(world):
    app = create_stub_app(world, ingest_key="test-key", auto_stop_after_s=0.2)
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    server = uvicorn.Server(uvicorn.Config(app, log_level="warning"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 10
        while not server.started and time.monotonic() < deadline:
            time.sleep(0.01)
        assert server.started
        base = f"http://127.0.0.1:{sock.getsockname()[1]}"
        with httpx.Client(base_url=base, headers={"X-Ingest-Key": "test-key"}) as client:
            assert client.get("/health").json() == {"status": "ok", "stub": True}
            assert positions(client, world).json()["lanes"]["TRUCK1"] == "RWY_04_22"
            assert send_audio(client).status_code == 202
            assert say(client, "LGA_GND", "GROUND", CROSSING).status_code == 202
            since = outbound(client)["now"]
            while not (calls := outbound(client, since)["calls"]) and time.monotonic() < deadline:
                time.sleep(0.05)
            assert [(c["action"], c["target"], c["text"]) for c in calls] == [
                ("stop", ["TRUCK1"], STOP_TEXT)]
            assert outbound(client, since=calls[0]["t"])["calls"] == []
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        sock.close()
