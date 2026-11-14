"""Incident: a runway crossing during a landing (a LaGuardia-style reconstruction).

AC8646 is cleared to land on runway 04 by Tower. About ``lead_s`` seconds before it passes
taxiway D, Ground clears fire truck TRUCK1 to cross runway 04 at D. The truck reads back
and rolls. If Hold Short gets a stop to the truck in time it holds short; if not, it
crosses in front of the landing aircraft.
"""

from __future__ import annotations

import copy
from typing import Any

from hssim.actors import Actor, Leg
from hssim.engine import Sim
from hssim.geo import KT
from hssim.scenarios.blocks import (
    GND,
    Airfield,
    apron_patrol,
    arrival,
    taxi_traffic,
    weather_reports,
)

RUNWAY_HALF_WIDTH_M = 22.5
REQUEST = "LaGuardia ground, Truck one and company, at Delta, request to cross runway four."
CLEARANCE = "Truck one and company, cross runway four at Delta."
READBACK = "Cross runway four at Delta, Truck one and company."
HOLDING = "Truck one is holding short of runway four."
RESUME_READBACK = "Crossing runway four at Delta, Truck one."
CLEAR = "Truck one and company, clear of runway four."


def seconds_until(actor: Actor, done: Any, limit_s: float = 900.0) -> float:
    """How long until ``done(clone)`` is true if nothing disturbs the actor."""
    clone = copy.deepcopy(actor)
    t = 0.0
    while t < limit_s and not done(clone):
        clone.step(0.2)
        t += 0.2
    return t


async def run(sim: Sim, opts: dict[str, Any]) -> dict[str, Any]:
    af, w = Airfield(sim.world), sim.world
    lead_s = float(opts.get("lead_s", 25))
    if opts.get("background", True):
        sim.flow(weather_reports(sim), name="weather", daemon=True)
        sim.flow(apron_patrol(sim, af), name="apron", daemon=True)
        sim.flow(taxi_traffic(sim, af, "UA1532"), name="repo", daemon=True)

    truck = sim.spawn("TRUCK1", "vehicle", callsign="TRUCK1", label="Fire truck and company",
                      status="ground", position=af.HP_D_NW,
                      heading_deg=w.frame.bearing_deg(af.HP_D_NW, af.D), accel=1.3, decel=3.5)
    to_centre = w.frame.distance_m(af.HP_D_NW, af.D)
    edge_s = to_centre - RUNWAY_HALF_WIDTH_M          # where the truck's path meets the runway
    exit_s = to_centre + RUNWAY_HALF_WIDTH_M
    result: dict[str, Any] = {"lead_s": lead_s, "stop_received": False, "stop_latency_s": None,
                              "truck_entered_runway": False, "min_separation_m": None,
                              "outcome": "pending"}
    state = {"clear_end": None, "t0": sim.sim_t}

    def on_stop(actor: Actor, call: dict[str, Any]) -> None:
        if result["stop_received"]:
            return
        result["stop_received"] = True
        if state["clear_end"] is not None:
            result["stop_latency_s"] = round(sim.sim_t - state["clear_end"], 1)
        braking = actor.v * actor.v / (2 * actor.decel)
        if actor.leg_name == "cross" and actor.s + braking >= edge_s and actor.s < exit_s:
            # too late to stop short: stopping on the runway would be worse, so keep going
            actor.legs[0].speed_kt = 25
            result["outcome"] = "stop_too_late"
        elif not actor.finished("cross"):
            actor.hold()
            result["outcome"] = "held_short"

    truck.stop_policy = on_stop  # type: ignore[attr-defined]

    landing = sim.flow(arrival(sim, af, "AC8646", start_nm=4.0, sequenced=False, stand=1,
                               tail=not opts.get("short", False)), name="arrival")
    await sim.until(lambda: sim.by_callsign("AC8646") is not None)
    jet = sim.by_callsign("AC8646")
    assert jet is not None
    state["t0"] = sim.sim_t
    t_pass = seconds_until(jet, lambda a: a.finished("rollout"))

    async def watch() -> None:
        while not jet.removed and not truck.removed:
            d = sim.separation_m(jet, truck)
            if result["min_separation_m"] is None or d < result["min_separation_m"]:
                result["min_separation_m"] = round(d, 1)
            if truck.leg_name == "cross" and edge_s <= truck.s <= exit_s:
                result["truck_entered_runway"] = True
            await sim.sleep(0.2)

    sim.flow(watch(), name="separation", daemon=True)

    # synthesise the three calls now so their real lengths can be planned around
    d_req = await sim.duration("TRUCK1", REQUEST)
    d_clr = await sim.duration(w.station(GND), CLEARANCE)
    await sim.duration("TRUCK1", READBACK)
    clear_end = t_pass - lead_s
    await sim.until(lambda: sim.sim_t - state["t0"] >= clear_end - d_clr - d_req - 2.4)
    await sim.say(GND, "TRUCK1", REQUEST)
    await sim.atc(GND, CLEARANCE)
    state["clear_end"] = sim.sim_t - 0.7
    await sim.say(GND, "TRUCK1", READBACK)
    await sim.sleep(0.5)

    if not truck.held:
        # reach the runway edge about 6 s before the aircraft passes D
        available = max(4.0, (t_pass - 6) - (sim.sim_t - state["t0"]) - 2.0)
        speed_kt = max(4.0, min(20.0, edge_s / available / KT))
        truck.go(Leg(w.path(af.HP_D_NW, af.D, af.HP_D_SE), speed_kt, name="cross"),
                 Leg(w.path(af.HP_D_SE, af.D_B), 12, end_speed_kt=0, name="park"))

    await sim.until(lambda: truck.held or truck.finished("cross") or landing.done())
    if truck.held:
        await sim.say(GND, "TRUCK1", HOLDING)
        # wait for the landing to be over, then for a controller-approved instruction to cross
        await sim.until(lambda: jet.removed or jet.finished("rollout") or landing.done(),
                        timeout=180)
        resumed = await sim.until(
            lambda: any(c.get("action") != "stop" and "cross" in (c.get("text") or "").lower()
                        for c in truck.calls),
            timeout=float(opts.get("resume_wait_s", 45)))
        if resumed:
            truck.release()
            if truck.idle:
                truck.go(Leg(w.path(truck.position, af.D, af.HP_D_SE), 14, name="cross"),
                         Leg(w.path(af.HP_D_SE, af.D_B), 12, end_speed_kt=0, name="park"))
            await sim.say(GND, "TRUCK1", RESUME_READBACK)
            await sim.until(lambda: truck.finished("cross"))
            await sim.say(GND, "TRUCK1", CLEAR)
            result["outcome"] = "held_short_then_crossed_when_approved"
    else:
        await sim.until(lambda: truck.finished("cross"))
        await sim.say(GND, "TRUCK1", CLEAR)
        if result["outcome"] == "pending":
            result["outcome"] = "crossed_in_front_of_landing"

    result["landing"] = await landing
    result["ok"] = result["outcome"].startswith("held_short")
    return result
