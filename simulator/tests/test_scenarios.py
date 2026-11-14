"""Every scenario, played fast against the stand-in tower through the real HTTP client.

The stub runs in-process (httpx ASGI transport), so these need no network and no tower.
Time is compressed with ``speed``; timestamps on the wire are still the real clock.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest

from hssim.cli import collect_lines, main
from hssim.client import TowerClient
from hssim.engine import Sim
from hssim.phrases import find_callsigns
from hssim.scenarios import SCENARIOS, normal
from hssim.stub_tower import create_stub_app
from hssim.world import World

SPEED = 60.0


def play(name: str, *, auto_stop: float | None = None, during: Any = None,
         **opts: Any) -> tuple[Any, Sim, Any]:
    """Run one scenario. ``during(sim, store)`` is an optional coroutine run alongside it.
    Returns (outcome, sim, stub store)."""

    async def go() -> tuple[Any, Sim, Any]:
        app = create_stub_app(auto_stop_after_s=auto_stop)
        store = app.state.store
        client = TowerClient("http://stub", transport=httpx.ASGITransport(app=app))
        try:
            sim = Sim(client, World(await client.world()), audio=False, speed=SPEED)
            scenario = SCENARIOS[name]
            side = asyncio.ensure_future(during(sim, store)) if during else None
            outcome = await sim.run(lambda s: scenario.run(s, {"cycles": 1, **opts}),
                                    scenario=name, kind=scenario.kind)
            if side is not None:
                side.cancel()
                await asyncio.gather(side, return_exceptions=True)
            return outcome, sim, store
        finally:
            await client.aclose()

    return asyncio.run(go())


def lanes_of(store: Any, object_id: str) -> set[str | None]:
    """Every lane the tower put this object on during the run."""
    world = World.load_default()
    out: set[str | None] = set()
    for batch in store.position_batches:
        for o in batch["objects"]:
            if o["object_id"] == object_id:
                out.add(world.lane_at(o["lon"], o["lat"], o["status"], o.get("alt_ft")))
    return out


def no_errors(outcome: Any) -> None:
    bad = {k: v for k, v in outcome.stats.items() if k.startswith("errors") or k == "flow_errors"}
    assert not bad, bad


# ------------------------------------------------------------------ crossing


def test_crossing_without_help_ends_in_a_conflict():
    outcome, _, store = play("crossing", short=True)
    no_errors(outcome)
    r = outcome.result
    assert r["outcome"] == "crossed_in_front_of_landing" and r["ok"] is False
    assert r["truck_entered_runway"] is True
    assert r["min_separation_m"] < 40
    assert "RWY_04_22" in lanes_of(store, "TRUCK1")
    kinds = {run["kind"] for run in store.runs}
    assert kinds == {"incident"} and store.runs[0]["ended_at"]


def test_crossing_truck_holds_short_when_the_stop_comes_in_time():
    # the stub issues the stop 0.1 s of real time (6 simulated seconds) after the clearance
    outcome, sim, store = play("crossing", auto_stop=0.1, short=True)
    no_errors(outcome)
    r = outcome.result
    assert r["stop_received"] and r["outcome"] == "held_short" and r["ok"]
    assert r["truck_entered_runway"] is False
    assert r["min_separation_m"] > 60
    assert "RWY_04_22" not in lanes_of(store, "TRUCK1")
    assert any("holding short" in s.text.lower() for s in sim.said if s.speaker == "TRUCK1")
    assert r["landing"] == "landed"


def test_crossing_clearance_comes_about_lead_seconds_before_the_conflict():
    outcome, sim, _ = play("crossing", lead_s=40, short=True)
    said = {s.text: s for s in sim.said}
    clearance = next(s for t, s in said.items() if t.startswith("Truck one and company, cross"))
    landing = next(s for t, s in said.items() if "cleared to land" in t)
    # the landing clearance is on Tower and comes first; the crossing clearance is on Ground
    assert landing.stream_id == "LGA_TWR" and clearance.stream_id == "LGA_GND"
    assert landing.t_start < clearance.t_start
    assert outcome.result["lead_s"] == 40


def test_a_late_stop_never_parks_the_truck_on_the_runway():
    # stop arrives about 20 simulated seconds after the clearance: the truck is at the edge
    outcome, _, store = play("crossing", auto_stop=20 / SPEED, short=True)
    r = outcome.result
    assert r["stop_received"]
    last = [o for o in store.position_batches[-1]["objects"] if o["object_id"] == "TRUCK1"]
    if last:  # still reported: it must not be sitting on the runway
        world = World.load_default()
        assert world.lane_at(last[0]["lon"], last[0]["lat"]) != "RWY_04_22" or \
            last[0]["speed_kt"] > 1


def test_the_held_truck_crosses_once_a_controller_approves_it():
    async def approve(sim: Sim, store: Any) -> None:
        while not any(c["action"] == "stop" for c in store.outbound):
            await asyncio.sleep(0.02)
        while "AC8646" in sim.actors and not sim.actors["AC8646"].finished("rollout"):
            await asyncio.sleep(0.02)
        store.add_outbound(action="propose_instruction", target=["TRUCK1"],
                           lane_id="RWY_04_22", stream_id="LGA_GND",
                           text="Truck one, cross runway zero four at Delta.")

    outcome, _, store = play("crossing", auto_stop=0.1, short=True, during=approve)
    assert outcome.result["outcome"] == "held_short_then_crossed_when_approved"
    assert "RWY_04_22" in lanes_of(store, "TRUCK1")


# -------------------------------------------------------------------- normal


def test_normal_traffic_is_quiet_sequenced_and_uses_three_streams():
    outcome, sim, store = play("normal")
    no_errors(outcome)
    assert outcome.result["ok"] and outcome.result["stops_heard"] == 0
    assert {c["stream_id"] for c in store.clips} == {"LGA_TWR", "LGA_GND", "LGA_OPS"}
    assert {run["kind"] for run in store.runs} == {"normal"}

    # the runway is used by one movement at a time, as the tower would hear it: a
    # controller gives the next runway clearance only after the previous user has reported
    # clear of the runway (or airborne)
    known = [*normal.ARRIVALS, *normal.DEPARTURES, *normal.VEHICLES]
    holder: str | None = None
    grants = 0
    for s in sorted(sim.said, key=lambda s: s.t_start):
        text = s.text.lower()
        if s.speaker in ("TOWER", "GROUND") and (
                "cleared to land" in text or "cleared for takeoff" in text
                or "cross runway four at delta" in text):
            assert holder is None, f"{s.text!r} while {holder} still had the runway"
            holder = find_callsigns(s.text, known)[0]
            grants += 1
        elif s.speaker == holder and ("clear of runway four" in text or "airborne" in text):
            holder = None
    assert holder is None and grants == 3   # one landing, one takeoff, one crossing
    kinds = {o["kind"] for b in store.position_batches for o in b["objects"]}
    assert kinds == {"aircraft", "vehicle"}


def test_calls_never_overlap_on_one_stream_but_do_across_streams():
    _, sim, _ = play("normal")
    by_stream: dict[str, list] = {}
    for s in sim.said:
        by_stream.setdefault(s.stream_id, []).append(s)
    spans = []
    for stream, said in by_stream.items():
        said.sort(key=lambda s: s.t_start)
        for a, b in zip(said, said[1:]):
            end_a = a.t_start.timestamp() + a.duration_s / SPEED
            assert end_a <= b.t_start.timestamp() + 1e-3, (stream, a.text, b.text)
        spans += [(s.t_start.timestamp(), s.t_start.timestamp() + s.duration_s / SPEED, stream)
                  for s in said]
    overlapping = any(a[2] != b[2] and a[0] < b[1] and b[0] < a[1]
                      for i, a in enumerate(spans) for b in spans[i + 1:])
    assert overlapping, "different streams should carry calls at the same time"


def test_positions_arrive_once_a_simulated_second_with_real_timestamps():
    outcome, sim, store = play("crossing", short=True)
    batches = store.position_batches
    assert abs(len(batches) - sim.sim_t) <= 5
    now = datetime.now(UTC)
    for b in (batches[0], batches[-1]):
        t = datetime.fromisoformat(b["t"].replace("Z", "+00:00"))
        assert abs((now - t).total_seconds()) < 120
    jet = [o for b in batches for o in b["objects"] if o["object_id"] == "AC8646"]
    assert jet[0]["status"] == "airborne" and jet[0]["alt_ft"] > 1000
    assert jet[-1]["status"] == "ground"
    assert outcome.stats["position_batches"] == len(batches)


# ------------------------------------------------------------------ intruder


def test_intruder_person_is_silent_and_ends_up_on_the_runway():
    outcome, sim, store = play("intruder", short=True)
    no_errors(outcome)
    people = [o for b in store.position_batches for o in b["objects"]
              if o["object_id"] == "OBJ-17"]
    assert people and all(o["kind"] == "person" and o["callsign"] is None for o in people)
    assert "RWY_04_22" in lanes_of(store, "OBJ-17")
    assert not any("OBJ" in (c.get("speaker") or "") for c in store.clips)
    assert outcome.result["outcome"] == "landed_with_intruder_on_runway"
    assert outcome.result["ok"] is False


def test_aircraft_goes_around_when_told():
    async def tell(sim: Sim, store: Any) -> None:
        while not any(o.get("lane_id") == "RWY_04_22" for o in store.objects.values()
                      if o["object_id"] == "OBJ-17"):
            await asyncio.sleep(0.01)
        store.add_outbound(action="propose_instruction", target=["AC8646"],
                           lane_id="RWY_04_22", stream_id="LGA_TWR",
                           text="Air Canada eight six four six, go around.")

    outcome, sim, _ = play("intruder", short=True, during=tell)
    assert outcome.result["landing"] == "went_around"
    assert outcome.result["outcome"] == "aircraft_went_around" and outcome.result["ok"]
    assert any(s.text.startswith("Going around") for s in sim.said)


def test_unidentified_vehicle_vacates_on_a_stop_addressed_to_nobody():
    async def stop(sim: Sim, store: Any) -> None:
        while not any(o.get("lane_id") == "RWY_04_22" for o in store.objects.values()
                      if o["object_id"] == "OBJ-17"):
            await asyncio.sleep(0.01)
        store.add_outbound(action="stop", target=[], lane_id="RWY_04_22",
                           stream_id="LGA_TWR",
                           text="Vehicle on runway zero four, STOP. Vacate the runway "
                                "immediately.")

    outcome, _, _ = play("intruder", intruder="vehicle", short=True, during=stop)
    assert outcome.result["vacated"] is True


# ------------------------------------------------- degraded radio, low visibility


def test_degraded_radio_has_each_kind_of_bad_call_and_no_stop():
    outcome, sim, store = play("degraded_radio", short=True)
    no_errors(outcome)
    texts = [s.text for s in sim.said]
    assert any("Cleared to land runway one three" in t for t in texts)          # wrong readback
    assert any(t.startswith("Air Canada eight six four six, negative, runway four")
               for t in texts)                                                   # correction
    assert any(s.radio == "brutal" for s in sim.said)                            # unreadable
    assert any(s.speaker == "BLOCKED" and " // " in s.text for s in sim.said)    # stepped on
    wrong = next(s for s in sim.said if s.text.startswith("Reducing one six zero, Air Canada "
                                                          "eight six four six"))
    assert wrong.speaker == "AC8646"                                             # wrong aircraft
    callsigns = {o["callsign"] for b in store.position_batches for o in b["objects"]}
    assert {"AC8646", "AC8464"} <= callsigns
    assert outcome.result["ok"] and outcome.result["stops_heard"] == 0


def test_low_visibility_drops_under_a_mile_and_recovers():
    outcome, _, store = play("low_visibility", short=True)
    no_errors(outcome)
    vis = [w["visibility_sm"] for w in store.weather]
    assert min(vis) < 1.0 and vis[0] >= 1.0 and vis[-1] >= 3.0
    assert all(w.get("raw", "").startswith("KLGA ") for w in store.weather)
    assert outcome.result["movements"] == ["landed", "departed"]


# ----------------------------------------------------------------- tooling


def test_collect_lines_finds_every_line_including_reactions():
    lines = asyncio.run(collect_lines("crossing", {"short": True}))
    texts = [line[1] for line in lines if line[0] != "__overlap__"]
    assert "Truck one and company, cross runway four at Delta." in texts
    assert "Truck one is holding short of runway four." in texts
    assert "Crossing runway four at Delta, Truck one." in texts
    assert len(texts) == len(set(zip([line[0] for line in lines if line[0] != "__overlap__"],
                                     texts)))


@pytest.mark.parametrize("argv,code", [
    (["run", "crossing", "--stub", "--auto-stop", "0.1", "--speed", "60", "--no-audio",
      "--short"], 0),
    (["run", "crossing", "--stub", "--speed", "60", "--no-audio", "--short"], 3),
    (["list"], 0),
])
def test_command_line(argv, code, capsys):
    assert main(argv) == code
    out = capsys.readouterr().out
    assert "crossing" in out
