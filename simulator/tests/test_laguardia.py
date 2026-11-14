"""The LaGuardia reconstruction must reproduce the NTSB preliminary report's numbers, and
show what an early stop by name changes."""

from __future__ import annotations

import asyncio
import math
import struct
import wave
from typing import Any

import httpx
import pytest

from hssim import realaudio
from hssim.client import TowerClient
from hssim.engine import Sim
from hssim.scenarios import SCENARIOS
from hssim.scenarios import laguardia as lga
from hssim.stub_tower import create_stub_app
from hssim.world import World

SPEED = 40.0
FT = 0.3048
LEAD_IN = 10.0


def play(*, stop_at: float | None = None, speed: float = SPEED,
         **opts: Any) -> tuple[Any, Sim, Any, dict]:
    """Run the scenario; also sample the jet and Truck 1 at the report's timestamps.

    ``stop_at``: the stand-in tower tells Truck 1 to stop this many simulated seconds after
    the landing clearance (23:35:07); the simulator hears it on its next poll."""

    async def go() -> tuple[Any, Sim, Any, dict]:
        app = create_stub_app()
        client = TowerClient("http://stub", transport=httpx.ASGITransport(app=app))
        try:
            sim = Sim(client, World(await client.world()), audio=False, speed=speed)
            d_point = sim.world.point("D_CROSSING")
            seen: dict[float, dict] = {}
            sent: list[bool] = []

            def probe(dt: float) -> None:
                t = sim.clock_s - LEAD_IN
                if stop_at is not None and not sent and t >= stop_at:
                    sent.append(True)
                    app.state.store.add_outbound(
                        action="stop", target=["TRUCK1"], lane_id="RWY_04_22",
                        stream_id="LGA_TWR",
                        text="Truck one, STOP. Hold short runway zero four.")
                jet, truck = sim.actors.get("JZA646"), sim.actors.get("TRUCK1")
                for when in (117, 120, 125, 130, 133, 135):
                    if when not in seen and t >= when and jet and truck:
                        seen[when] = {
                            "jet_ft": sim.world.frame.distance_m(jet.position, d_point) / FT,
                            "alt": jet.alt_ft, "kt": jet.speed_kt, "status": jet.status,
                            "truck_mph": truck.speed_kt * 1.15078, "truck_s": truck.s,
                            "truck_leg": truck.leg_name}

            sim.tick_hooks.append(probe)
            outcome = await sim.run(
                lambda s: SCENARIOS["laguardia"].run(s, {"short": True, **opts}),
                scenario="laguardia", kind="incident")
            return outcome, sim, app.state.store, seen
        finally:
            await client.aclose()

    return asyncio.run(go())


def test_without_help_it_ends_as_the_record_does():
    outcome, sim, store, seen = play()
    r = outcome.result
    assert not {k for k in outcome.stats if k.startswith("errors") or k == "flow_errors"}
    assert r["outcome"] == "collision_as_on_record" and r["ok"] is False
    assert r["impact_speed_kt"] == 90 and r["truck_entered_runway"] is True
    assert r["min_separation_m"] < 25

    # the jet, against the report: (seconds after 23:35:07) -> feet from taxiway D, altitude
    for when, feet, alt in [(117, 4400, 130), (120, 3700, 87), (125, 2550, 30), (130, 1450, 0),
                            (135, 400, 0)]:
        assert seen[when]["jet_ft"] == pytest.approx(feet, abs=120), when
        assert seen[when]["alt"] == pytest.approx(alt, abs=8), when
    assert seen[125]["kt"] == pytest.approx(133, abs=5)
    assert seen[130]["kt"] == pytest.approx(128, abs=4) and seen[135]["kt"] == pytest.approx(106, abs=4)
    assert seen[125]["status"] == "airborne" and seen[135]["status"] == "ground"

    # Truck 1, against the report: stationary until it reads back, "just over 100 ft from
    # entering runway 4 at about 29 mph" at 23:37:20, "30 mph and just entering" at 23:37:22
    edge = lga.TRUCK1_START_FROM_EDGE_M
    assert seen[117]["truck_mph"] == 0 and seen[120]["truck_leg"] in ("", "cross")
    assert (edge - seen[133]["truck_s"]) / FT == pytest.approx(105, abs=25)
    assert seen[133]["truck_mph"] == pytest.approx(29, abs=2)
    assert seen[135]["truck_mph"] == pytest.approx(30, abs=1.5)
    assert abs(edge - seen[135]["truck_s"]) < 12


def test_the_record_is_what_the_tower_hears():
    _, sim, store, _ = play(speed=15.0)      # slower: call start times are checked closely
    tower = sorted((s for s in sim.said if s.stream_id == "LGA_TWR"), key=lambda s: s.t_start)
    landing = next(s for s in tower if "cleared to land" in s.text)
    clearance = next(s for s in tower if s.text == lga.CLEARANCE)
    stops = [s for s in tower if s.text in (lga.STOP_1, lga.STOP_2)]

    def after_landing(s: Any) -> float:
        return s.clock_s - LEAD_IN        # simulated seconds after 23:35:07

    # both clearances come from the tower controller on the tower frequency, 117 s apart
    assert landing.speaker == clearance.speaker == "TOWER"
    assert after_landing(landing) == pytest.approx(0, abs=2)
    assert after_landing(clearance) == pytest.approx(117, abs=4)
    assert [round(after_landing(s)) for s in stops] == pytest.approx([125, 133], abs=4)
    assert "Frontier four one niner five, just stop there please" in stops[0].text
    assert any(s.speaker == "BLOCKED" for s in tower)          # Truck 7's first call
    assert sum(s.text == lga.TRUCK7_CALL for s in tower) == 1  # ... and its second

    # seven vehicles, the real callsigns, the real weather report
    ids = {o["object_id"] for b in store.position_batches for o in b["objects"]}
    assert {c[0] for c in lga.CONVOY} <= ids and len(lga.CONVOY) == 7
    assert {"JZA646", "FFT4195", "UAL2384"} <= ids
    assert store.weather[0]["raw"].startswith("KLGA 230251Z 06007KT 7SM -RA")
    assert store.weather[0]["visibility_sm"] == 7


def test_a_stop_by_name_in_time_keeps_the_truck_off_the_runway():
    # the stop goes out 8 simulated seconds after the crossing clearance was issued
    outcome, sim, store, seen = play(stop_at=lga.T_CLEARANCE + 8)
    r = outcome.result
    assert r["stop_received"] and r["outcome"].startswith("held_short") and r["ok"]
    assert r["truck_entered_runway"] is False and r["impact_speed_kt"] is None
    assert r["stop_heard_after_s"] < r["stop_needed_within_s"]
    assert r["truck_stopped_m_from_runway"] > 5
    assert r["min_separation_m"] > 25
    # the controller's own stop calls still went out, as they did that night
    assert any(s.text == lga.STOP_1 for s in sim.said)
    assert any(s.text == lga.HOLDING for s in sim.said)


def test_a_stop_that_comes_too_late_changes_nothing():
    # 15.5 s after the clearance: Truck 1 can no longer stop short of the runway
    outcome, _, _, _ = play(stop_at=lga.T_CLEARANCE + 15.5, speed=20.0)
    r = outcome.result
    assert r["stop_received"] and r["stop_heard_after_s"] > r["stop_needed_within_s"]
    assert r["outcome"] == "collision_stop_too_late" and r["ok"] is False


def test_a_slower_crew_needs_the_stop_sooner():
    quick, _, _, _ = play(crew_reaction_s=1.0)
    slow, _, _, _ = play(crew_reaction_s=6.0)
    assert slow.result["stop_needed_within_s"] == pytest.approx(
        quick.result["stop_needed_within_s"] - 5.0, abs=0.3)


# ------------------------------------------------------------ real recording


def _recording(path, bursts: list[tuple[float, float]], total_s: float, rate: int = 8000):
    """A fake frequency recording: quiet hiss with a tone for each (start, length)."""
    frames = bytearray()
    for i in range(int(total_s * rate)):
        t = i / rate
        loud = any(a <= t < a + d for a, d in bursts)
        value = int(9000 * math.sin(2 * math.pi * 700 * t)) if loud else (i * 7919 % 41) - 20
        frames += struct.pack("<h", value)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(bytes(frames))


def test_bursts_are_found_and_cut(tmp_path, monkeypatch):
    monkeypatch.setattr(realaudio, "CACHE", tmp_path / "cache")
    rec = tmp_path / "KLGA-Twr-Mar-23-2026-0330Z.wav"
    _recording(rec, [(2.0, 3.0), (8.0, 1.5), (9.9, 1.0), (20.0, 0.1)], 24)
    found = realaudio.cut_bursts(rec)
    # the two calls 0.4 s apart are one transmission; the 0.1 s click is ignored
    assert [(round(b.offset_s), round(b.duration_s)) for b in found] == [(2, 3), (8, 3)]
    with wave.open(str(found[0].path)) as w:
        assert w.getnframes() / w.getframerate() == pytest.approx(found[0].duration_s, abs=0.05)
    assert realaudio.start_seconds_from_name(rec) == 3 * 3600 + 30 * 60
    assert realaudio.parse_clock("5:07") == 307 and realaudio.parse_clock("03:35:07") == 12907


def test_a_real_recording_replaces_the_generated_tower_calls(tmp_path, monkeypatch):
    monkeypatch.setattr(realaudio, "CACHE", tmp_path / "cache")
    rec = tmp_path / "tower.wav"
    offset = 30.0                       # the landing clearance is 30 s into this file
    calls = [(offset + 0, 3.0), (offset + 117, 2.0), (offset + 120, 2.5), (offset + 125, 6.0)]
    _recording(rec, calls, offset + 150)
    outcome, sim, store, _ = play(real_audio=rec, real_audio_offset=offset)
    tower_clips = [c for c in store.clips if c["stream_id"] == "LGA_TWR"]
    assert len(tower_clips) == 4 and all(c["kind"] == "audio" for c in tower_clips)
    assert all(not c["text"] and not c["speaker"] for c in tower_clips)   # the tower must listen
    assert not any(s.stream_id == "LGA_TWR" and s.text == lga.CLEARANCE for s in sim.said)
    # the other streams and the traffic are unchanged, so the ending is the recorded one
    assert {c["stream_id"] for c in store.clips} == {"LGA_TWR", "LGA_GND", "LGA_OPS"}
    assert outcome.result["outcome"] == "collision_as_on_record"
