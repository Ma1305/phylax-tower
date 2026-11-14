"""The scenarios the simulator can play, by name."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from hssim.engine import Sim
from hssim.scenarios import crossing, degraded_radio, intruder, low_visibility, normal


@dataclass(frozen=True)
class Scenario:
    name: str
    kind: str            # "normal" | "incident" (the run marker on the tower's timeline)
    summary: str
    expect: str          # what a working tower should do
    run: Callable[[Sim, dict[str, Any]], Awaitable[dict[str, Any]]]


SCENARIOS: dict[str, Scenario] = {s.name: s for s in [
    Scenario("normal", "normal",
             "Arrivals and departures on runway 04, a cleared vehicle crossing at D, apron "
             "traffic and weather, on the Tower, Ground and Ops streams.",
             "Nothing: no warning that matters and no stop.", normal.run),
    Scenario("crossing", "incident",
             "AC8646 is cleared to land on 04 by Tower; Ground clears fire truck TRUCK1 to "
             "cross 04 at D about 25 s before the aircraft gets there.",
             "A critical warning to the tower controller and a stop for TRUCK1 before it "
             "enters the runway; the truck holds short.", crossing.run),
    Scenario("intruder", "incident",
             "A person on foot (or, with --intruder vehicle, an unidentified vehicle) with "
             "no clearance and no radio call is on runway 04 while AC8646 is on final.",
             "A runway-incursion warning from positions alone; a stop call for the object "
             "on the runway and, if the controller chooses, a go-around.", intruder.run),
    Scenario("degraded_radio", "normal",
             "A wrong readback, two similar callsigns (AC8646 / AC8464) with the wrong one "
             "answering, an unreadable call and a stepped-on transmission.",
             "Flags and warnings on the calls (readback mismatch, unclear, confusable); "
             "no stop.", degraded_radio.run),
    Scenario("low_visibility", "normal",
             "Visibility falls under 1 statute mile during an arrival and a departure, then "
             "recovers.",
             "The weather agent switches to the low-visibility rule set and back.",
             low_visibility.run),
]}
