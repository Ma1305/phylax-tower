"""Incident: something with no clearance and no radio call is on the runway while an
aircraft is on final. Default: a person on foot (no callsign, no radio). ``intruder=vehicle``:
an unidentified vehicle with a radio but no callsign, which leaves when told to stop.

Nothing is said on the radio about the intruder: the tower has to see it in the positions.
"""

from __future__ import annotations

from typing import Any

from hssim.actors import Actor, Leg
from hssim.engine import Sim
from hssim.scenarios.blocks import Airfield, apron_patrol, arrival, weather_reports
from hssim.scenarios.crossing import RUNWAY_HALF_WIDTH_M, seconds_until


async def run(sim: Sim, opts: dict[str, Any]) -> dict[str, Any]:
    af, w = Airfield(sim.world), sim.world
    kind = "vehicle" if opts.get("intruder") == "vehicle" else "person"
    if opts.get("background", True):
        sim.flow(weather_reports(sim), name="weather", daemon=True)
        sim.flow(apron_patrol(sim, af), name="apron", daemon=True)

    landing = sim.flow(arrival(sim, af, "AC8646", start_nm=4.0, sequenced=False, stand=1,
                               tail=not opts.get("short", False)), name="arrival")
    await sim.until(lambda: sim.by_callsign("AC8646") is not None)
    jet = sim.by_callsign("AC8646")
    assert jet is not None
    t0 = sim.sim_t
    t_threshold = seconds_until(jet, lambda a: a.finished("final"))

    to_edge = w.frame.distance_m(af.HP_G_SE, af.G) - RUNWAY_HALF_WIDTH_M
    speed_kt = 3.2 if kind == "person" else 11.0
    walk_s = to_edge / (speed_kt * 0.514444)
    # step onto the runway when the aircraft is about 45 s from the threshold
    await sim.until(lambda: sim.sim_t - t0 >= max(0.0, t_threshold - 45 - walk_s))
    intruder = sim.spawn("OBJ-17", kind, callsign=None, status="ground", position=af.HP_G_SE,
                         label="Person on foot" if kind == "person" else "Unidentified vehicle",
                         accel=1.0, decel=2.5, has_radio=kind == "vehicle")
    result: dict[str, Any] = {"intruder": kind, "on_runway_at_touchdown": None,
                              "min_separation_m": None, "calls_heard": 0, "vacated": False}

    def on_stop(actor: Actor, call: dict[str, Any]) -> None:
        # a driver who hears "vacate the runway" turns back the way it came
        actor.clear_route()
        actor.go(Leg(w.path(actor.position, af.HP_G_SE), 15, end_speed_kt=0, name="vacate"))
        result["vacated"] = True

    intruder.stop_policy = on_stop  # type: ignore[attr-defined]
    intruder.go(Leg(w.path(af.HP_G_SE, af.G), speed_kt, name="onto_runway"),
                Leg(w.path(af.G, af.rwy(520)), speed_kt, end_speed_kt=0, name="along_runway"))

    def on_runway() -> bool:
        return w.lane_at(intruder.lon, intruder.lat) == "RWY_04_22"

    async def watch() -> None:
        while not jet.removed:
            d = sim.separation_m(jet, intruder)
            if result["min_separation_m"] is None or d < result["min_separation_m"]:
                result["min_separation_m"] = round(d, 1)
            if result["on_runway_at_touchdown"] is None and jet.finished("final"):
                result["on_runway_at_touchdown"] = on_runway()
            await sim.sleep(0.2)

    sim.flow(watch(), name="separation", daemon=True)
    result["landing"] = await landing
    result["calls_heard"] = len(sim.outbound_calls)
    if result["landing"] == "went_around":
        result["outcome"] = "aircraft_went_around"
    elif result["on_runway_at_touchdown"]:
        result["outcome"] = "landed_with_intruder_on_runway"
    else:
        result["outcome"] = "runway_clear_at_touchdown"
    result["ok"] = result["outcome"] != "landed_with_intruder_on_runway"
    return result
