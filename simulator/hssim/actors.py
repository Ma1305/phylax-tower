"""Things that move: aircraft, vehicles, people. An actor follows a queue of legs."""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass
from typing import Any

from hssim.geo import KT, LonLat, Path


@dataclass
class Leg:
    """One stretch of an actor's route."""

    path: Path
    speed_kt: float                     # speed to hold on this leg
    end_speed_kt: float | None = None   # speed to reach by the end (None: keep going)
    alt_start_ft: float | None = None
    alt_end_ft: float | None = None
    status: str | None = None           # "airborne" / "ground", applied when the leg starts
    name: str = ""


class Actor:
    def __init__(self, object_id: str, kind: str, *, position: LonLat,
                 callsign: str | None = None, label: str | None = None,
                 status: str = "ground", heading_deg: float = 0.0,
                 alt_ft: float | None = None, speed_kt: float = 0.0,
                 accel: float = 1.5, decel: float = 2.0, has_radio: bool = True):
        self.object_id = object_id
        self.kind = kind
        self.callsign = callsign
        self.label = label
        self.status = status
        self.lon, self.lat = position
        self.heading_deg = heading_deg
        self.alt_ft = alt_ft
        self.v = speed_kt * KT            # m/s
        self.accel = accel                # m/s^2
        self.decel = decel
        self.has_radio = has_radio
        self.legs: deque[Leg] = deque()
        self.s = 0.0                      # metres along the current leg
        self.held = False                 # told to stop: brakes to a halt and stays
        self.removed = False
        self.done: list[str] = []         # names of finished legs
        self.calls: list[dict[str, Any]] = []   # outbound calls addressed to this actor
        self.go_around = False
        self.lane_id: str | None = None   # as the tower last reported it
        self.odometer_m = 0.0

    # ------------------------------------------------------------------ route
    def go(self, *legs: Leg) -> None:
        self.legs.extend(legs)

    def clear_route(self) -> None:
        self.legs.clear()
        self.s = 0.0

    @property
    def leg(self) -> Leg | None:
        return self.legs[0] if self.legs else None

    @property
    def leg_name(self) -> str:
        return self.legs[0].name if self.legs else ""

    @property
    def idle(self) -> bool:
        return not self.legs

    @property
    def speed_kt(self) -> float:
        return self.v / KT

    @property
    def position(self) -> LonLat:
        return (self.lon, self.lat)

    def hold(self) -> None:
        self.held = True

    def release(self) -> None:
        self.held = False

    # ----------------------------------------------------------------- motion
    def step(self, dt: float) -> None:
        """Advance ``dt`` seconds."""
        if self.removed:
            return
        leg = self.leg
        if leg is None:
            if self.status == "ground" or self.held:
                self.v = max(0.0, self.v - self.decel * dt)
            return
        if self.s == 0.0 and leg.status:
            self.status = leg.status
        remaining = leg.path.length_m - self.s
        target = 0.0 if self.held else leg.speed_kt * KT
        if leg.end_speed_kt is not None and not self.held:
            v_end = leg.end_speed_kt * KT
            target = min(target, math.sqrt(max(0.0, v_end * v_end + 2 * self.decel * remaining)))
        if self.v < target:
            self.v = min(target, self.v + self.accel * dt)
        else:
            self.v = max(target, self.v - self.decel * dt)
        move = self.v * dt
        self.odometer_m += move
        self.s += move
        while self.legs and self.s >= self.legs[0].path.length_m - 1e-6:
            finished = self.legs.popleft()
            self.s -= finished.path.length_m
            self.done.append(finished.name)
            self.lon, self.lat = finished.path.at(finished.path.length_m)
            if finished.alt_end_ft is not None:
                self.alt_ft = finished.alt_end_ft
            if not self.legs:
                self.s = 0.0
                if finished.end_speed_kt is not None:
                    self.v = finished.end_speed_kt * KT
                elif self.status == "ground":
                    self.v = 0.0
                return
            if self.legs[0].status:
                self.status = self.legs[0].status
        leg = self.legs[0]
        self.lon, self.lat = leg.path.at(self.s)
        if self.v > 0.2:
            self.heading_deg = leg.path.heading_at(self.s)
        if leg.alt_start_ft is not None and leg.alt_end_ft is not None:
            f = 0.0 if leg.path.length_m == 0 else min(1.0, self.s / leg.path.length_m)
            self.alt_ft = leg.alt_start_ft + (leg.alt_end_ft - leg.alt_start_ft) * f
        elif self.status == "ground":
            self.alt_ft = 0.0 if self.kind == "aircraft" else None

    def finished(self, leg_name: str) -> bool:
        return leg_name in self.done

    # ------------------------------------------------------------------- wire
    def payload(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "object_id": self.object_id, "kind": self.kind, "callsign": self.callsign,
            "status": self.status, "lat": round(self.lat, 6), "lon": round(self.lon, 6),
            "heading_deg": round(self.heading_deg, 1) % 360, "speed_kt": round(self.speed_kt, 1),
        }
        if self.label:
            out["label"] = self.label
        if self.alt_ft is not None:
            out["alt_ft"] = round(self.alt_ft)
        return out
