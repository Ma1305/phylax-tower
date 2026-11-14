"""Normal traffic: arrivals and departures on runway 04, a vehicle crossing at D with a
proper clearance, an ops vehicle on the apron, weather reports. Spread over the Tower,
Ground and Ops streams. A correct tower stays quiet: this must cause no stop.
"""

from __future__ import annotations

import asyncio
from typing import Any

from hssim.engine import Sim
from hssim.scenarios.blocks import (
    Airfield,
    apron_patrol,
    arrival,
    departure,
    vehicle_crossing,
    weather_reports,
)

ARRIVALS = ["AC8646", "UA1532", "AC712", "AC6846"]
DEPARTURES = ["DL2214", "AC305", "UA233", "AC1190"]
VEHICLES = ["TRUCK2", "TRUCK3"]
PERIOD_S = 240


async def _later(sim: Sim, delay_s: float, coro: Any) -> Any:
    await sim.sleep(delay_s)
    return await coro


def start_background(sim: Sim, af: Airfield) -> None:
    if sim.shared.get("background_started"):
        return
    sim.shared["background_started"] = True
    sim.flow(weather_reports(sim), name="weather", daemon=True)
    sim.flow(apron_patrol(sim, af), name="apron", daemon=True)


def start_cycle(sim: Sim, af: Airfield, i: int) -> list[asyncio.Task]:
    """One arrival, one departure and one vehicle crossing, sequenced over the runway."""
    return [
        sim.flow(arrival(sim, af, ARRIVALS[i % len(ARRIVALS)], stand=i % 2),
                 name=f"arrival{i}"),
        sim.flow(_later(sim, 12, departure(sim, af, DEPARTURES[i % len(DEPARTURES)],
                                           stand=2 + i % 2)), name=f"departure{i}"),
        sim.flow(_later(sim, 150, vehicle_crossing(sim, af, VEHICLES[i % len(VEHICLES)])),
                 name=f"crossing{i}"),
    ]


async def run(sim: Sim, opts: dict[str, Any]) -> dict[str, Any]:
    from hssim.scenarios import SCENARIOS  # late: the registry imports this module

    af = Airfield(sim.world)
    start_background(sim, af)
    cycles, loop = int(opts.get("cycles", 1)), bool(opts.get("loop", False))
    inject: asyncio.Queue | None = sim.shared.get("inject_queue")
    injected: list[dict[str, Any]] = []
    tasks: list[asyncio.Task] = []
    i = 0
    while loop or i < cycles:
        if inject is not None and not inject.empty():
            name = inject.get_nowait()
            sim.shared["status"] = f"waiting for traffic to finish, then {name}"
            await asyncio.gather(*tasks, return_exceptions=True)
            sim.shared["status"] = f"running {name}"
            run_id = await sim._safe(sim.client.run_start(name, SCENARIOS[name].kind,
                                                          "injected into normal traffic"),
                                     "run_start")
            result = await SCENARIOS[name].run(sim, {**opts, "background": False,
                                                     "short": True})
            if run_id:
                await sim._safe(sim.client.run_end(run_id), "run_end")
            injected.append({"scenario": name, **result})
            sim.shared["status"] = "normal traffic"
            continue
        tasks = [t for t in tasks if not t.done()] + start_cycle(sim, af, i)
        i += 1
        if not loop and i >= cycles:
            break
        for _ in range(PERIOD_S):
            if inject is not None and not inject.empty():
                break
            await sim.sleep(1)
    await asyncio.gather(*tasks, return_exceptions=True)
    stops = [c for c in sim.outbound_calls if c.get("action") == "stop"]
    return {"cycles": i, "stops_heard": len(stops), "injected": injected,
            "ok": not stops or bool(injected)}
