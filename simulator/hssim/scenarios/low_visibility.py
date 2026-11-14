"""Low visibility: during normal traffic the visibility drops under 1 statute mile. The
tower's weather agent should ask for the low-visibility rule set. Visibility recovers at
the end so the next scenario starts from normal rules.
"""

from __future__ import annotations

from typing import Any

from hssim.engine import Sim
from hssim.scenarios.blocks import TWR, Airfield, apron_patrol, arrival, departure
from hssim.scenarios.normal import _later


def metar(sim: Sim, visibility: str, wind: str, sky: str) -> str:
    return f"KLGA {sim.utcnow():%d%H%M}Z {wind} {visibility} {sky} 08/07 A2992"


async def run(sim: Sim, opts: dict[str, Any]) -> dict[str, Any]:
    af = Airfield(sim.world)
    if opts.get("background", True):
        sim.flow(apron_patrol(sim, af), name="apron", daemon=True)
    await sim.weather(3.0, "03012KT", metar(sim, "3SM", "03012KT", "BR BKN008"))
    landing = sim.flow(arrival(sim, af, "AC8646", start_nm=6.0, stand=0,
                               tail=not opts.get("short", False)), name="arrival")
    takeoff = sim.flow(_later(sim, 12, departure(sim, af, "DL2214", stand=2)),
                       name="departure")
    await sim.sleep(40)
    await sim.weather(0.75, "02008KT", metar(sim, "3/4SM", "02008KT", "BR OVC003"))
    await sim.atc(TWR, "Attention all aircraft, low visibility procedures are now in effect "
                       "at LaGuardia.")
    await sim.sleep(60)
    await sim.weather(0.5, "02006KT", metar(sim, "1/2SM", "02006KT", "FG OVC002"))
    results = [await landing, await takeoff]
    await sim.weather(5.0, "03010KT", metar(sim, "5SM", "03010KT", "BKN015"))
    stops = [c for c in sim.outbound_calls if c.get("action") == "stop"]
    return {"movements": results, "stops_heard": len(stops),
            "expect": "the weather agent switches the active rule set to low_vis_v1, then "
                      "back to normal_v1 when visibility recovers",
            "ok": True}
