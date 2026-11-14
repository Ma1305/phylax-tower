"""Building blocks every scenario is made of: the airfield's useful points, the runway
sequencing token, and the standard movements (arrival, departure, a vehicle crossing,
apron traffic, weather reports), each with correct radio calls on the right stream.
"""

from __future__ import annotations

import itertools
from typing import Any

from hssim.actors import Actor, Leg
from hssim.engine import Sim
from hssim.geo import NM, LonLat
from hssim.phrases import spoken_callsign
from hssim.world import World

TWR, GND, OPS = "LGA_TWR", "LGA_GND", "LGA_OPS"
GLIDE_FT_PER_NM = 318.0
SETTLE_S = 8.0   # after a "clear of runway" / "airborne" report, before the runway is reused
COUNT = ["zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten",
         "eleven", "twelve"]


class Airfield:
    """Named places and paths on the seeded LaGuardia schematic."""

    def __init__(self, world: World):
        self.world = world
        f = world.frame
        self.thr = world.point("RWY04_THRESHOLD")
        self.far = world.point("RWY22_THRESHOLD")
        self.rwy_brg = f.bearing_deg(self.thr, self.far)
        self.rwy_len = f.distance_m(self.thr, self.far)
        self.D = world.point("D_CROSSING")
        self.G = world.point("G_CROSSING")
        self.HP_D_SE, self.HP_D_NW = world.point("HP_D_SE"), world.point("HP_D_NW")
        self.HP_G_SE, self.HP_G_NW = world.point("HP_G_SE"), world.point("HP_G_NW")
        self.D_B, self.D_NW = self._ends("TWY_D", self.HP_D_SE)
        self.G_B, self.G_NW = self._ends("TWY_G", self.HP_G_SE)
        self.fire_station = world.points.get("FIRE_STATION", self.D_NW)
        self._b = world.path(*world.lanes["TWY_B"].centerline)  # type: ignore[misc]
        ring = world.lanes["APRON_MAIN"].ring
        self._inner, self._outer = (ring[0], ring[1]), (ring[3], ring[2])

    def _ends(self, lane_id: str, near: LonLat) -> tuple[LonLat, LonLat]:
        a, b = self.world.lanes[lane_id].centerline[0], self.world.lanes[lane_id].centerline[-1]  # type: ignore[index]
        d = self.world.frame.distance_m
        return (a, b) if d(a, near) < d(b, near) else (b, a)

    def final(self, nm: float) -> LonLat:
        """A point ``nm`` nautical miles out on the extended centreline of runway 04."""
        return self.world.frame.offset(self.thr, self.rwy_brg + 180, nm * NM)

    def rwy(self, metres: float) -> LonLat:
        """A point on the runway centreline, ``metres`` from the 04 threshold."""
        return self.world.frame.offset(self.thr, self.rwy_brg, metres)

    def beyond(self, nm: float) -> LonLat:
        return self.world.frame.offset(self.far, self.rwy_brg, nm * NM)

    def stand(self, i: int) -> LonLat:
        """Parking stand ``i`` on the main apron."""
        f = self.world.frame
        frac = 0.18 + 0.16 * (i % 5)
        inner = f.along(self._inner[0], self._inner[1], frac)
        outer = f.along(self._outer[0], self._outer[1], frac)
        return f.along(inner, outer, 0.45)

    def b_abeam(self, p: LonLat) -> LonLat:
        """The point on taxiway B closest to ``p``."""
        return self._b.at(self._b.distance_to_point(p))

    def dist_to_threshold_nm(self, actor: Actor) -> float:
        return self.world.frame.distance_m(actor.position, self.thr) / NM


class Runway:
    """Who may use runway 04 next. Lower ``priority`` goes first among those waiting."""

    def __init__(self) -> None:
        self.owner: str | None = None
        self._waiting: list[tuple[int, int, str]] = []
        self._seq = itertools.count()

    @property
    def free(self) -> bool:
        return self.owner is None

    async def acquire(self, sim: Sim, name: str, priority: int, give_up: Any = None) -> bool:
        entry = (priority, next(self._seq), name)
        self._waiting.append(entry)
        try:
            while True:
                if self.owner is None and min(self._waiting) == entry:
                    self.owner = name
                    return True
                if give_up is not None and give_up():
                    return False
                await sim.sleep(0.3)
        finally:
            self._waiting.remove(entry)

    def release(self, name: str) -> None:
        if self.owner == name:
            self.owner = None


def runway(sim: Sim) -> Runway:
    return sim.shared.setdefault("runway", Runway())


# ------------------------------------------------------------------- arrival


async def arrival(sim: Sim, af: Airfield, cs: str, *, start_nm: float = 6.0, stand: int = 0,
                  label: str = "A320", clear_at_nm: float = 3.6, sequenced: bool = True,
                  wrong_readback: bool = False, tail: bool = True) -> str:
    """An aircraft flies the final to runway 04, lands, vacates at D and taxis to a stand.

    ``sequenced``: wait for the runway token before the landing clearance (normal traffic).
    ``tail``: do the after-landing calls and the taxi to the stand.
    Returns "landed" or "went_around".
    """
    w, s = sim.world, spoken_callsign(cs)
    rw = runway(sim)
    alt0 = start_nm * GLIDE_FT_PER_NM
    ac = sim.spawn(cs, "aircraft", callsign=cs, label=label, status="airborne",
                   position=af.final(start_nm), alt_ft=alt0, heading_deg=af.rwy_brg,
                   speed_kt=140, accel=1.2, decel=3.0)
    ac.go(Leg(w.path(af.final(start_nm), af.thr), 140, end_speed_kt=132, alt_start_ft=alt0,
              alt_end_ft=15, status="airborne", name="final"),
          Leg(w.path(af.thr, af.D), 132, end_speed_kt=14, status="ground", name="rollout"),
          Leg(w.path(af.D, af.HP_D_SE, af.D_B), 14, end_speed_kt=0, name="vacate"))

    await sim.sleep(3)
    miles = COUNT[min(12, max(1, round(start_nm)))]
    await sim.say(TWR, cs, f"LaGuardia tower, {s}, {miles} mile final, runway four.")

    holding_token = False
    if sequenced:
        if not rw.free or af.dist_to_threshold_nm(ac) > clear_at_nm + 0.3:
            await sim.atc(TWR, f"{s}, LaGuardia tower, continue approach, runway four.")
            await sim.until(lambda: af.dist_to_threshold_nm(ac) <= clear_at_nm)
        holding_token = await rw.acquire(
            sim, cs, 0, give_up=lambda: af.dist_to_threshold_nm(ac) < 1.2 or ac.go_around)
        if not holding_token:
            ac.go_around = True
            if not ac.calls:
                await sim.atc(TWR, f"{s}, go around, runway not clear.")
    if not ac.go_around:
        await sim.atc(TWR, f"{s}, wind zero three zero at one two, runway four, "
                           "cleared to land.")
        if wrong_readback:
            await sim.say(TWR, cs, f"Cleared to land runway one three, {s}.")
            await sim.atc(TWR, f"{s}, negative, runway four. Runway four, cleared to land.")
        await sim.say(TWR, cs, f"Cleared to land runway four, {s}.")

    await sim.until(lambda: ac.finished("final") or ac.go_around)
    if ac.go_around and not ac.finished("final") and (ac.alt_ft or 0) > 60:
        return await _go_around(sim, af, ac, rw if holding_token else None)

    await sim.until(lambda: ac.finished("rollout"))
    await sim.until(lambda: ac.finished("vacate") or (ac.leg_name == "vacate" and ac.s > 112))
    await sim.say(TWR, cs, f"{s}, clear of runway four at Delta.")
    sim.flow(_release_later(sim, rw, cs, holding_token), name=f"release:{cs}")
    if not tail:
        await sim.until(lambda: ac.finished("vacate"))
        return "landed"
    await sim.atc(TWR, f"{s}, roger, contact ground, one two one point seven.")
    await sim.say(GND, cs, f"LaGuardia ground, {s}, clear of runway four at Delta, "
                           "request taxi to the ramp.")
    await sim.atc(GND, f"{s}, LaGuardia ground, taxi to the ramp via Bravo.")
    await sim.say(GND, cs, f"Taxi to the ramp via Bravo, {s}.")
    await sim.until(lambda: ac.finished("vacate"))
    spot = af.stand(stand)
    ac.go(Leg(w.path(af.D_B, af.b_abeam(spot), spot), 16, end_speed_kt=0, name="taxi_in"))
    await sim.until(lambda: ac.finished("taxi_in"))
    await sim.sleep(8)
    await sim.remove(ac, "parked at the gate")
    return "landed"


async def _release_later(sim: Sim, rw: Runway, name: str, holding: bool) -> None:
    await sim.sleep(SETTLE_S)
    if holding:
        rw.release(name)


async def _go_around(sim: Sim, af: Airfield, ac: Actor, rw: Runway | None) -> str:
    s = spoken_callsign(ac.callsign or ac.object_id)
    ac.clear_route()
    ac.status = "airborne"
    ac.accel = 2.0
    ac.go(Leg(sim.world.path(ac.position, af.beyond(3.0)), 160, alt_start_ft=ac.alt_ft or 100,
              alt_end_ft=2500, status="airborne", name="go_around"))
    await sim.say(TWR, ac.callsign or ac.object_id, f"Going around, {s}.")
    if rw is not None:
        rw.release(ac.callsign or ac.object_id)
    await sim.until(lambda: ac.finished("go_around"))
    await sim.remove(ac, "left the area after a go-around")
    return "went_around"


# ----------------------------------------------------------------- departure


async def departure(sim: Sim, af: Airfield, cs: str, *, stand: int = 2,
                    label: str = "B737") -> str:
    """An aircraft taxis from its stand via B to G, holds short, and departs runway 04."""
    w, s = sim.world, spoken_callsign(cs)
    rw = runway(sim)
    spot = af.stand(stand)
    ac = sim.spawn(cs, "aircraft", callsign=cs, label=label, status="ground", position=spot,
                   heading_deg=af.rwy_brg + 180, alt_ft=0, accel=1.0, decel=1.6)
    await sim.sleep(4)
    await sim.say(GND, cs, f"LaGuardia ground, {s}, at the ramp, ready to taxi.")
    await sim.atc(GND, f"{s}, LaGuardia ground, runway four at Golf, taxi via Bravo, "
                       "hold short runway four.")
    await sim.say(GND, cs, f"Runway four at Golf via Bravo, hold short runway four, {s}.")
    ac.go(Leg(w.path(spot, af.b_abeam(spot), af.G_B, af.HP_G_SE), 17, end_speed_kt=0,
              name="taxi_out"))
    await sim.until(lambda: ac.finished("taxi_out"))
    await sim.say(TWR, cs, f"LaGuardia tower, {s}, holding short runway four at Golf, "
                           "ready for departure.")
    if not rw.free:
        await sim.atc(TWR, f"{s}, LaGuardia tower, hold short runway four, landing traffic.")
        await sim.say(TWR, cs, f"Holding short runway four, {s}.")
    await rw.acquire(sim, cs, 1)
    await sim.atc(TWR, f"{s}, wind zero three zero at one two, runway four at Golf, "
                       "cleared for takeoff.")
    await sim.say(TWR, cs, f"Cleared for takeoff runway four, {s}.")
    lift = af.rwy(min(af.rwy_len - 250, 1800))
    ac.accel = 2.3
    ac.go(Leg(w.path(af.HP_G_SE, af.G), 12, end_speed_kt=8, name="line_up"),
          Leg(w.path(af.G, lift), 160, status="ground", name="takeoff_roll"),
          Leg(w.path(lift, af.beyond(3.0)), 175, alt_start_ft=0, alt_end_ft=2600,
              status="airborne", name="climb"))
    await sim.until(lambda: ac.finished("takeoff_roll") and (ac.alt_ft or 0) > 350)
    await sim.atc(TWR, f"{s}, contact departure.")
    await sim.say(TWR, cs, f"{s}, airborne, switching to departure.")
    sim.flow(_release_later(sim, rw, cs, True), name=f"release:{cs}")
    await sim.until(lambda: ac.finished("climb"))
    await sim.remove(ac, "left the area")
    return "departed"


# ---------------------------------------------------------- vehicle crossing


async def vehicle_crossing(sim: Sim, af: Airfield, cs: str, *, label: str = "Airport vehicle",
                           stream: str = GND) -> str:
    """A vehicle leaves the fire station side, asks to cross runway 04 at D, waits for a
    clearance that is only given when the runway is quiet, crosses and parks on the apron."""
    w, s = sim.world, spoken_callsign(cs)
    rw = runway(sim)
    station = w.station(stream)
    v = sim.spawn(cs, "vehicle", callsign=cs, label=label, status="ground",
                  position=af.fire_station, accel=1.5, decel=3.0)
    v.go(Leg(w.path(af.fire_station, af.HP_D_NW), 12, end_speed_kt=0, name="to_hold"))
    await sim.until(lambda: v.finished("to_hold"))
    await sim.say(stream, cs, f"LaGuardia {station.lower()}, {s}, at Delta, "
                              "request cross runway four.")
    if not rw.free:
        await sim.atc(stream, f"{s}, hold short runway four.")
        await sim.say(stream, cs, f"Holding short runway four, {s}.")
    await rw.acquire(sim, cs, 2)
    await sim.atc(stream, f"{s}, cross runway four at Delta.")
    await sim.say(stream, cs, f"Cross runway four at Delta, {s}.")
    spot = af.stand(4)
    v.go(Leg(w.path(af.HP_D_NW, af.D, af.HP_D_SE), 14, name="cross"),
         Leg(w.path(af.HP_D_SE, af.D_B, af.b_abeam(spot), spot), 14, end_speed_kt=0,
             name="park"))
    await sim.until(lambda: v.finished("cross"))
    await sim.say(stream, cs, f"{s}, clear of runway four.")
    sim.flow(_release_later(sim, rw, cs, True), name=f"release:{cs}")
    await sim.until(lambda: v.finished("park"))
    await sim.sleep(5)
    await sim.remove(v, "parked on the apron")
    return "crossed"


# --------------------------------------------------------- background traffic


async def apron_patrol(sim: Sim, af: Airfield, cs: str = "OPS1",
                       label: str = "Ops vehicle") -> None:
    """An operations vehicle drives between stands and checks in on the Ops stream."""
    w, s = sim.world, spoken_callsign(cs)
    v = sim.spawn(cs, "vehicle", callsign=cs, label=label, status="ground",
                  position=af.stand(1), accel=1.2, decel=2.5)
    reports = ["stand three inspection complete, no debris.",
               "apron lighting check complete.",
               "wildlife check complete, apron clear.",
               "fuel spill check at stand one, all clear."]
    for i in itertools.count():
        a, b = af.stand((i * 2 + 1) % 5), af.stand((i * 2 + 3) % 5)
        v.go(Leg(w.path(a, b), 11, end_speed_kt=0, name=f"patrol{i}"))
        await sim.until(lambda i=i: v.finished(f"patrol{i}"))
        await sim.say(OPS, cs, f"LaGuardia ops, {s}, {reports[i % len(reports)]}")
        await sim.atc(OPS, f"{s}, ops, roger.")
        await sim.sleep(18)


async def taxi_traffic(sim: Sim, af: Airfield, cs: str, *, label: str = "E175") -> None:
    """A repositioning aircraft taxis along B between two stands (never near the runway)."""
    w, s = sim.world, spoken_callsign(cs)
    a, b = af.stand(0), af.stand(3)
    ac = sim.spawn(cs, "aircraft", callsign=cs, label=label, status="ground", position=a,
                   alt_ft=0, accel=0.9, decel=1.5)
    await sim.say(GND, cs, f"LaGuardia ground, {s}, request reposition to stand four.")
    await sim.atc(GND, f"{s}, LaGuardia ground, taxi to stand four via Bravo.")
    await sim.say(GND, cs, f"Taxi to stand four via Bravo, {s}.")
    ac.go(Leg(w.path(a, af.b_abeam(a), af.b_abeam(b), b), 14, end_speed_kt=0, name="repo"))
    await sim.until(lambda: ac.finished("repo"))


async def weather_reports(sim: Sim, visibility_sm: float = 10.0, wind: str = "03012KT",
                          every_s: float = 60.0) -> None:
    while True:
        await sim.weather(visibility_sm, wind)
        await sim.sleep(every_s)
