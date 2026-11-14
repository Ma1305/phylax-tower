"""Degraded radio during otherwise normal traffic: a wrong readback, two similar callsigns
on one stream with the wrong aircraft answering, an unreadable call, and a stepped-on
(blocked) transmission. No runway conflict: the tower should flag, not stop.
"""

from __future__ import annotations

from typing import Any

from hssim.engine import Sim
from hssim.scenarios.blocks import GND, TWR, Airfield, apron_patrol, arrival, weather_reports

UNCLEAR = ("Ground, Truck two, uh, we're at, uh... Delta, uh... for the, uh, the runway... "
           "say again?")


async def run(sim: Sim, opts: dict[str, Any]) -> dict[str, Any]:
    af = Airfield(sim.world)
    if opts.get("background", True):
        sim.flow(weather_reports(sim), name="weather", daemon=True)
        sim.flow(apron_patrol(sim, af), name="apron", daemon=True)

    # 1. wrong readback: cleared to land runway four, read back as runway one three
    first = sim.flow(arrival(sim, af, "AC8646", start_nm=6.0, wrong_readback=True, stand=0,
                             tail=not opts.get("short", False)), name="arrival1")
    sim.spawn("DL2214", "aircraft", callsign="DL2214", label="B737", status="ground",
              position=af.stand(3), alt_ft=0)
    sim.spawn("TRUCK2", "vehicle", callsign="TRUCK2", label="Fire truck", status="ground",
              position=af.fire_station)

    # 2. a similar callsign behind it on the same frequency
    await sim.sleep(20)
    second = sim.flow(arrival(sim, af, "AC8464", start_nm=11.0, stand=1,
                              tail=not opts.get("short", False)), name="arrival2")

    # 3. an unreadable call on Ground
    await sim.sleep(15)
    await sim.say(GND, "TRUCK2", UNCLEAR, radio="brutal")
    await sim.atc(GND, "Station calling ground, say again.")
    await sim.say(GND, "TRUCK2", "LaGuardia ground, Truck two, at the fire station, "
                                 "radio check.")
    await sim.atc(GND, "Truck two, loud and clear.")

    # 4. the wrong aircraft takes an instruction meant for the similar callsign
    await sim.sleep(10)
    await sim.atc(TWR, "Air Canada eight four six four, reduce speed to one six zero.")
    await sim.say(TWR, "AC8646", "Reducing one six zero, Air Canada eight six four six.")
    await sim.atc(TWR, "Air Canada eight six four six, negative, that was for Air Canada "
                       "eight four six four. Air Canada eight four six four, reduce speed "
                       "to one six zero.")
    await sim.say(TWR, "AC8464", "Reducing one six zero, Air Canada eight four six four.")

    # 5. two stations transmit at once
    await sim.sleep(12)
    await sim.say_overlap(
        TWR,
        ("AC8464", "LaGuardia tower, Air Canada eight four six four, seven mile final."),
        ("DL2214", "Tower, Delta two two one four, at the ramp, request clearance."))
    await sim.atc(TWR, "Blocked. Station calling tower, say again.")
    await sim.say(TWR, "AC8464", "LaGuardia tower, Air Canada eight four six four, "
                                 "seven mile final, runway four.")

    outcomes = [await first, await second]
    stops = [c for c in sim.outbound_calls if c.get("action") == "stop"]
    return {"landings": outcomes, "stops_heard": len(stops),
            "expect_flags": ["readback mismatch", "unclear - say again",
                             "confusable callsigns AC8646 / AC8464", "blocked transmission"],
            "ok": not stops}
