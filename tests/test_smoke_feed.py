"""tools/smoke_feed.py against a fake tower (httpx.MockTransport). No network, no database."""

import importlib.util
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[1]
WORLD = json.loads((ROOT / "docs" / "v2" / "world_lga.json").read_text())

spec = importlib.util.spec_from_file_location("smoke_feed", ROOT / "tools" / "smoke_feed.py")
smoke_feed = importlib.util.module_from_spec(spec)
spec.loader.exec_module(smoke_feed)


class FakeClock:
    def __init__(self):
        self.t = datetime(2026, 11, 14, 19, 0, tzinfo=UTC)

    def now(self):
        return self.t

    def sleep(self, s):
        if s > 0:
            self.t += timedelta(seconds=s)


class FakeTower:
    def __init__(self, clock, stop_after_s=None):
        self.clock = clock
        self.start = clock.now()
        self.stop_after_s = stop_after_s
        self.calls = []  # (method, path, payload)

    def handler(self, req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if req.method == "GET" and path == "/ingest/world":
            self.calls.append(("GET", path, None))
            return httpx.Response(200, json=WORLD)
        if req.method == "GET" and path == "/ingest/outbound":
            now = self.clock.now()
            calls = []
            el = (now - self.start).total_seconds()
            if self.stop_after_s is not None and el >= self.stop_after_s:
                calls = [{"t": now.isoformat(), "decision_id": "d1", "stream_id": "LGA_GND",
                          "action": "stop", "target": ["TRUCK1"], "lane_id": "RWY_04_22",
                          "text": "Truck one, STOP. Hold short runway zero four."}]
            return httpx.Response(200, json={"now": now.isoformat(), "calls": calls})
        if path == "/ingest/audio":
            body = req.read().decode("latin-1")
            meta = json.loads(body.split('name="meta"')[1].split("\r\n\r\n", 1)[1]
                              .split("\r\n--", 1)[0])
            self.calls.append(("POST", path, meta))
            return httpx.Response(202, json={"clip_id": "c"})
        payload = json.loads(req.read() or b"{}")
        self.calls.append((req.method, path, payload))
        if path == "/ingest/runs":
            return httpx.Response(200, json={"run_id": "r1"})
        if path == "/ingest/positions":
            return httpx.Response(200, json={"accepted": len(payload["objects"]), "lanes": {}})
        return httpx.Response(202 if path == "/ingest/transcripts" else 200, json={})

    def posted(self, path):
        return [p for m, pth, p in self.calls if pth == path]


@pytest.fixture(autouse=True)
def _no_tts(monkeypatch):
    monkeypatch.setenv("VOICE_ENABLED", "false")
    monkeypatch.delenv("ELEVENLABS_API_KEY", raising=False)


def _parse(t):
    return datetime.fromisoformat(t.replace("Z", "+00:00"))


def _truck_track(tower):
    return [next(o for o in p["objects"] if o["object_id"] == "TRUCK1")
            for p in tower.posted("/ingest/positions")]


def test_incident_positions_clips_and_stop():
    clock = FakeClock()
    tower = FakeTower(clock, stop_after_s=37)
    rc = smoke_feed.main(["--api", "http://tower", "--scenario", "incident"],
                         transport=httpx.MockTransport(tower.handler), clock=clock)
    assert rc == 0
    paths = [c[1] for c in tower.calls]
    assert paths.index("/ingest/reset") < paths.index("/ingest/runs")
    assert paths[-1] == "/ingest/runs/r1/end"
    pos = tower.posted("/ingest/positions")
    assert len(pos) >= 70
    ts = [_parse(p["t"]) for p in pos]
    gaps = [(b - a).total_seconds() for a, b in zip(ts, ts[1:], strict=False)]
    assert all(0.9 <= g <= 1.1 for g in gaps)
    clips = tower.posted("/ingest/audio")
    assert len({c["stream_id"] for c in clips}) >= 2
    assert all(c["truth_text"] and c["speaker"] for c in clips)
    truck = _truck_track(tower)
    last = truck[-1]
    hold = WORLD["hold_points"][1]["position"]["coordinates"]  # HP_D_NW
    cross = WORLD["points"]["D_CROSSING"]
    # held: never reached the runway centerline, stopped at speed 0
    assert last["speed_kt"] == 0
    assert smoke_feed.dist_m([last["lon"], last["lat"]], hold) < \
        smoke_feed.dist_m([last["lon"], last["lat"]], cross)


def test_incident_without_stop_crosses():
    clock = FakeClock()
    tower = FakeTower(clock, stop_after_s=None)
    smoke_feed.main(["--api", "http://tower", "--scenario", "incident", "--no-audio"],
                    transport=httpx.MockTransport(tower.handler), clock=clock)
    last = _truck_track(tower)[-1]
    se = WORLD["hold_points"][0]["position"]["coordinates"]  # HP_D_SE
    assert smoke_feed.dist_m([last["lon"], last["lat"]], se) < 5


def test_normal_finishes_fast_no_audio():
    tower = FakeTower(smoke_feed.Clock())
    rc = smoke_feed.main(["--api", "http://tower", "--scenario", "normal", "--no-audio",
                          "--speed", "20"], transport=httpx.MockTransport(tower.handler))
    assert rc == 0
    assert tower.posted("/ingest/audio") == []
    tx = tower.posted("/ingest/transcripts")
    assert {t["stream_id"] for t in tx} == {"LGA_TWR", "LGA_GND", "LGA_OPS"}
    assert tower.calls[-1][1] == "/ingest/runs/r1/end"
    ids = {o["object_id"] for p in tower.posted("/ingest/positions") for o in p["objects"]}
    assert ids == {"AC8646", "DL2214", "OPS7"}


def test_both_and_intruder_load():
    w = smoke_feed.World(WORLD)
    both = smoke_feed.load_scenario("both")
    assert {o["object_id"] for o in both["objects"]} == {"AC8646", "TRUCK1", "OBJ-17"}
    for name in ("normal", "incident", "intruder"):
        for o in smoke_feed.load_scenario(name)["objects"]:
            smoke_feed.Mover(o, w)
