"""Reconstruction of the runway 4 / taxiway D collision at LaGuardia on 22 March 2026.

Source: NTSB Aviation Investigation Preliminary Report DCA26MA161 (23 April 2026, public,
preliminary and subject to change). Every time, distance, altitude and speed in the tables
below is the report's. The airport geometry is the tower's seeded schematic, so positions are
placed by their distance from taxiway D, which is how the report gives them.

From the record:
- 23:35:07 the local (tower) controller cleared Jazz 646 (a CRJ-900 flying as Air Canada
  Express 8646) to land on runway 4. That evening the same controller was transmitting on
  both the tower and the ground frequency while the ground controller handled another
  aircraft's emergency at terminal B.
- Seven vehicles responding to that emergency gathered on taxiway D west of the runway.
  "Truck 7" called the tower twice; the first call was blocked by another transmission.
- 23:37:04 "Truck 1 and company" were instructed to cross runway 4 at taxiway D with the
  jet about 130 ft above the ground and 4,400 ft from taxiway D. Truck 1 read back and moved.
- 23:37:12 and 23:37:20 the controller told Truck 1 to stop. Its speed kept increasing.
- About 23:37:24 the jet, at about 90 knots, struck Truck 1 at the intersection.

The truck's request, the clearance, the readback and the stop calls follow the wording
news outlets transcribed from the tower recording (with the word "runway" kept in, which the
recording drops: "cross four at Delta"). The other calls are reconstructed in standard
phraseology. The calls on the Ground and Ops streams about the other
emergency are invented context, not from the record.

What the simulator adds: if Hold Short tells Truck 1 to stop by name, the crew brakes after
``crew_reaction_s``. The controller's own stop calls are played as they happened and, as on
the night, do not stop the truck.
"""

from __future__ import annotations

import copy
import logging
from typing import Any

from hssim.actors import Actor, Leg
from hssim.engine import Sim
from hssim.geo import KT
from hssim.scenarios.blocks import GND, OPS, TWR, Airfield

log = logging.getLogger("hssim")
FT = 0.3048
RUNWAY_HALF_WIDTH_M = 22.5
T0_UTC_S = 3 * 3600 + 35 * 60 + 7          # 23:35:07 EDT = 03:35:07 UTC, the landing clearance

# seconds after the landing clearance -> (distance from taxiway D in feet, altitude in feet)
TRACK = [(0, 33155, 1900), (21, 27950, 1500), (40, 22480, 1180), (74, 13975, 650),
         (97, 9115, 400), (104, 7290, 287), (117, 4400, 130), (120, 3700, 87),
         (125, 2550, 30), (130, 1450, 0), (135, 400, 0), (137.4, 0, 0)]
# recorded ground speeds near the end (seconds -> knots)
SPEEDS = [(125, 133), (130, 128), (135, 106), (137.4, 90)]

T_CONVOY_LEAVES, T_BLOCKED_CALL, T_TRUCK7_AGAIN = 21, 40, 74
T_WHICH_VEHICLE, T_TRUCK1_IN_FRONT, T_TRUCK1_REPLY = 97, 104, 109
T_CLEARANCE, T_READBACK, T_STOP_1, T_TOUCHDOWN, T_STOP_2, T_IMPACT = 117, 120, 125, 130, 133, 137.4

TRUCK1_START_FROM_EDGE_M = 460 * FT        # "stopped on taxiway D about 460 feet from edge"
TRUCK_ACCEL = 1.3                          # fits 10 kt after 4 s and 30 mph at the runway edge
TRUCK_TOP_KT = 26.0                        # 30 mph
METAR = ("KLGA 230251Z 06007KT 7SM -RA SCT060 OVC095 09/08 A2973 RMK AO2 SLP067 P0003 60007 "
         "T00940078 50022 $")              # the 22:51 local observation, the last before it

JET = "Jazz six four six"
CHECK_IN = f"LaGuardia tower, {JET}, five mile final, runway four."
LAND = f"{JET}, LaGuardia tower, runway four, cleared to land."
LAND_READBACK = f"Cleared to land runway four, {JET}."
TRUCK7_CALL = "LaGuardia tower, Truck seven."
BLOCKER = "Tower, Frontier four one niner five, on Bravo."
WHICH_VEHICLE = "Vehicle needing to cross the runway, say your call sign."
TRUCK1_REPLY = "Truck one and company."
TRUCK1_ACK = "Truck one and company, LaGuardia tower."
REQUEST = "Truck one and company, LaGuardia tower, requesting to cross runway four at Delta."
CLEARANCE = "Truck one and company, cross runway four at Delta."
READBACK = "Truck one and company, crossing runway four at Delta."
STOP_1 = ("Frontier four one niner five, just stop there please. Stop, stop, stop, stop. "
          "Truck one, stop, stop, stop.")          # after the published recording
STOP_2 = "Stop, Truck one, stop. Stop, Truck one, stop!"
HOLDING = "Truck one and company, holding short of runway four."
RESUME_READBACK = "Crossing runway four at Delta, Truck one and company."
CLEAR = "Truck one and company, clear of runway four."
JET_CLEAR = f"{JET}, clear of runway four."

CONVOY = [  # object id, callsign, label: seven vehicles, as in the report
    ("TRUCK7", "TRUCK7", "Tool truck (Truck 7)"),
    ("TRUCK1", "TRUCK1", "Rescue 35 (Truck 1), ARFF truck"),
    ("ARFF-2", None, "ARFF truck"),
    ("ARFF-3", None, "ARFF truck"),
    ("ARFF-4", None, "ARFF truck"),
    ("STAIR-1", None, "Airstair truck"),
    ("PAPD-1", None, "Port Authority police"),
]
FOLLOW_GAP_M = 10.0


def _segment(table: list[tuple[float, ...]], t: float) -> tuple[tuple[float, ...], ...]:
    """The two rows around ``t`` (the first or last pair when ``t`` is outside the table)."""
    for a, b in zip(table, table[1:]):
        if t <= b[0]:
            return a, b
    return table[-2], table[-1]


def _interp(table: list[tuple[float, ...]], t: float, col: int = 1) -> float:
    a, b = _segment(table, t)
    return a[col] + (b[col] - a[col]) * (t - a[0]) / (b[0] - a[0])


def _slope(table: list[tuple[float, ...]], t: float, col: int = 1) -> float:
    a, b = _segment(table, t)
    return (b[col] - a[col]) / (b[0] - a[0])


async def run(sim: Sim, opts: dict[str, Any]) -> dict[str, Any]:
    af, w = Airfield(sim.world), sim.world
    frame = w.frame
    reaction_s = float(opts.get("crew_reaction_s", 2.0))
    real_audio = opts.get("real_audio")
    lead_in = 10.0
    t0 = sim.clock_s + lead_in

    def now() -> float:
        return sim.clock_s - t0

    async def at(t: float) -> None:
        await sim.until(lambda: now() >= t, poll=0.1)

    west = frame.bearing_deg(af.D, af.HP_D_NW)      # along taxiway D towards the fire station

    def slot(d_m: float) -> tuple[float, float]:
        """The point on taxiway D, ``d_m`` metres west of the runway centreline."""
        return frame.offset(af.D, west, d_m)

    start_d = TRUCK1_START_FROM_EDGE_M + RUNWAY_HALF_WIDTH_M
    edge_s = TRUCK1_START_FROM_EDGE_M                 # distance Truck 1 drives to the runway edge
    exit_s = edge_s + 2 * RUNWAY_HALF_WIDTH_M
    result: dict[str, Any] = {
        "source": "NTSB preliminary report DCA26MA161",
        "crew_reaction_s": reaction_s, "stop_received": False, "stop_heard_after_s": None,
        "stop_needed_within_s": None, "truck_entered_runway": False,
        "truck_stopped_m_from_runway": None, "min_separation_m": None,
        "impact_speed_kt": None, "outcome": "pending"}
    state: dict[str, Any] = {"frozen": False, "started": None}

    await sim.weather(7.0, "06007KT", METAR)

    # ---- the jet: its position follows the recorded track until it reaches taxiway D
    jet = sim.spawn("JZA646", "aircraft", callsign="JZA646", status="airborne",
                    label="CRJ-900 (Air Canada Express 8646)", heading_deg=af.rwy_brg,
                    position=frame.offset(af.D, af.rwy_brg + 180, TRACK[0][1] * FT),
                    alt_ft=TRACK[0][2], accel=1.0, decel=2.6)

    def fly(dt: float) -> None:
        t = now()
        if t >= T_IMPACT:
            sim.tick_hooks.remove(fly)
            state["jet_at_d"] = True
            return
        d_ft = max(0.0, _interp(TRACK, t, 1))
        jet.lon, jet.lat = frame.offset(af.D, af.rwy_brg + 180, d_ft * FT)
        jet.alt_ft = max(0.0, _interp(TRACK, t, 2))
        jet.status = "airborne" if t < T_TOUCHDOWN else "ground"
        jet.heading_deg = af.rwy_brg
        jet.v = (_interp(SPEEDS, t) * KT) if t >= SPEEDS[0][0] else -_slope(TRACK, t) * FT

    sim.tick_hooks.append(fly)

    # ---- other traffic on the ground that night
    sim.spawn("UAL2384", "aircraft", callsign="UAL2384", status="ground", alt_ft=0,
              position=af.stand(3), label="B737 (ground emergency at terminal B)")
    sim.spawn("OPS1", "vehicle", callsign="OPS1", status="ground",
              position=frame.offset(af.stand(3), 200, 30), label="Ops vehicle")
    # The report says another airplane that had landed on runway 4 was rolling past taxiway D
    # at 23:35:47. Here it has already vacated and is taxiing on B, so the tower is not shown
    # a landing clearance onto an occupied runway that the record does not treat as a conflict.
    exit_pt = af.b_abeam(af.rwy(1500))
    frontier = sim.spawn("FFT4195", "aircraft", callsign="FFT4195", status="ground", alt_ft=0,
                         position=exit_pt, heading_deg=(af.rwy_brg + 180) % 360, speed_kt=8,
                         label="A320 (landed earlier on runway 4)", accel=0.8, decel=1.5)
    frontier.go(Leg(w.path(exit_pt, af.b_abeam(af.stand(0)), af.stand(0)), 9, end_speed_kt=0,
                    name="taxi"))

    async def frontier_stops() -> None:      # "Frontier 4195, just stop there please"
        await at(T_STOP_1 + 2.5)
        frontier.hold()
        await sim.sleep(40)
        frontier.release()

    sim.flow(frontier_stops(), name="frontier", daemon=True)

    # ---- the seven vehicles
    convoy: list[Actor] = []
    truck: Actor | None = None

    async def vehicles() -> None:
        nonlocal truck
        await at(T_CONVOY_LEAVES)
        for i, (oid, callsign, label) in enumerate(CONVOY):
            v = sim.spawn(oid, "vehicle", callsign=callsign, label=label, status="ground",
                          position=slot(230 + 13 * i), heading_deg=(west + 180) % 360,
                          accel=TRUCK_ACCEL, decel=3.5)
            v.go(Leg(w.path(slot(230 + 13 * i), slot(178 + 13 * i)), 12, end_speed_kt=0,
                     name="gather"))
            convoy.append(v)
        truck = convoy[1]
        truck.stop_policy = on_stop  # type: ignore[attr-defined]
        await at(T_WHICH_VEHICLE)            # Truck 1 moves to the front of the group
        convoy.insert(0, convoy.pop(1))
        truck.go(Leg(w.path(truck.position, slot(start_d)), 10, end_speed_kt=0, name="to_front"))

    def follow(dt: float) -> None:
        for ahead, v in zip(convoy, convoy[1:]):
            gap = frame.distance_m(ahead.position, v.position)
            v.held = bool(state["frozen"]) or gap < FOLLOW_GAP_M

    sim.tick_hooks.append(follow)
    sim.flow(vehicles(), name="vehicles")

    # ---- Hold Short's stop, addressed to Truck 1 by name
    def on_stop(actor: Actor, call: dict[str, Any]) -> None:
        if result["stop_received"] or state["frozen"]:
            return
        result["stop_received"] = True
        result["stop_heard_after_s"] = round(now() - T_CLEARANCE, 1)
        sim.flow(react(), name="crew_reaction")

    async def react() -> None:
        await sim.sleep(reaction_s)
        assert truck is not None
        braking = truck.v * truck.v / (2 * truck.decel)
        if truck.finished("cross") or state["frozen"]:
            return
        if truck.leg_name == "cross" and truck.s + braking >= edge_s:
            result["outcome"] = "stop_too_late"      # cannot stop short of the runway any more
            return
        truck.hold()
        result["outcome"] = "held_short"

    # ---- radio
    async def tower_frequency() -> None:
        if real_audio:
            return                                    # the real recording carries this stream
        await at(-8)
        await sim.say(TWR, "JZA646", CHECK_IN)
        await at(0)
        await sim.atc(TWR, LAND)
        await sim.say(TWR, "JZA646", LAND_READBACK)
        await at(T_BLOCKED_CALL)
        await sim.say_overlap(TWR, ("TRUCK7", TRUCK7_CALL), ("FFT4195", BLOCKER), delay_s=0.3)
        await at(T_TRUCK7_AGAIN)
        await sim.say(TWR, "TRUCK7", TRUCK7_CALL)
        await at(T_WHICH_VEHICLE)
        await sim.atc(TWR, WHICH_VEHICLE)
        await at(T_TRUCK1_REPLY)
        await sim.say(TWR, "TRUCK1", TRUCK1_REPLY, gap_s=0.2)
        await sim.atc(TWR, TRUCK1_ACK, gap_s=0.2)
        await sim.say(TWR, "TRUCK1", REQUEST, gap_s=0.2)
        await sim.atc(TWR, CLEARANCE, gap_s=0.2)
        await sim.say(TWR, "TRUCK1", READBACK, gap_s=0.2)
        await at(T_STOP_1)
        await sim.atc(TWR, STOP_1, gap_s=0.2)
        await at(T_STOP_2)
        await sim.atc(TWR, STOP_2)

    async def recording() -> None:
        from hssim.realaudio import cut_bursts

        offset = float(opts["real_audio_offset"])     # seconds into the file of 23:35:07
        bursts = cut_bursts(real_audio, offset - 12, offset + T_STOP_2 + 6)
        log.info("real recording: %d transmissions between -12 s and +%d s of the landing "
                 "clearance", len(bursts), T_STOP_2 + 6)
        for b in bursts:
            await at(b.offset_s - offset)
            started = sim.utcnow()
            log.info("%-8s %-7s (real recording, %.1f s)", TWR, "REAL", b.duration_s)
            await sim.sleep(b.duration_s)
            sim._post_utterance(TWR, None, "", started, b.duration_s, b)

    async def other_emergency() -> None:              # invented context, not from the record
        await at(6)
        await sim.atc(GND, "United two three eight four, LaGuardia ground, the fire crews are "
                           "on their way to you at terminal Bravo.")
        await sim.say(GND, "UAL2384", "United two three eight four, roger, holding position.")
        await at(55)
        await sim.say(OPS, "OPS1", "LaGuardia ops, Ops one, standing by at terminal Bravo for "
                                   "the United aircraft.")
        await sim.atc(OPS, "Ops one, ops, roger.")
        await at(88)
        await sim.atc(GND, "United two three eight four, ground, say your intentions.")
        await sim.say(GND, "UAL2384", "United two three eight four, we would like the fire "
                                      "crews to check the aircraft before we deplane.")

    sim.flow(tower_frequency(), name="tower")
    if real_audio:
        sim.flow(recording(), name="recording")
    sim.flow(other_emergency(), name="context", daemon=True)

    # ---- Truck 1 rolls as it reads back the clearance
    await at(T_READBACK)
    assert truck is not None
    spot = af.stand(4)
    if not truck.held:
        clone = copy.deepcopy(truck)
        clone.go(Leg(w.path(slot(start_d), af.D, af.HP_D_SE), TRUCK_TOP_KT, name="cross"))
        latest, limit = 0.0, 60.0
        while latest < limit and clone.s + clone.v * clone.v / (2 * clone.decel) < edge_s:
            clone.step(0.1)
            latest += 0.1
        result["stop_needed_within_s"] = round(T_READBACK - T_CLEARANCE + latest - reaction_s, 1)
        truck.go(Leg(w.path(truck.position, af.D, af.HP_D_SE), TRUCK_TOP_KT, name="cross"),
                 Leg(w.path(af.HP_D_SE, af.D_B, af.b_abeam(spot), spot), 20, end_speed_kt=0,
                     name="to_terminal"))
        state["started"] = now()

    async def company() -> None:
        for v in convoy[1:]:
            await sim.sleep(2.5)
            if state["frozen"] or truck.held:
                return
            v.go(Leg(w.path(v.position, af.D, af.HP_D_SE), TRUCK_TOP_KT, name="cross"),
                 Leg(w.path(af.HP_D_SE, af.D_B, af.b_abeam(spot), spot), 20, end_speed_kt=0,
                     name="to_terminal"))

    sim.flow(company(), name="company", daemon=True)

    def watch(dt: float) -> None:
        d = sim.separation_m(jet, truck)
        if result["min_separation_m"] is None or d < result["min_separation_m"]:
            result["min_separation_m"] = round(d, 1)
        if (not state.get("jet_at_d") and truck.leg_name == "cross"
                and edge_s <= truck.s <= exit_s):
            result["truck_entered_runway"] = True     # ahead of the landing jet

    sim.tick_hooks.append(watch)
    await sim.until(lambda: state.get("jet_at_d", False), poll=0.1)

    on_runway = truck.leg_name == "cross" and edge_s - 4 <= truck.s <= exit_s + 4
    if on_runway:
        # as on the record: the jet reaches taxiway D with Truck 1 on the runway
        state["frozen"] = True
        result["outcome"] = "collision_as_on_record" if not result["stop_received"] \
            else "collision_stop_too_late"
        result["impact_speed_kt"] = SPEEDS[-1][1]
        truck.clear_route()
        truck.v = 0.0
        truck.hold()
        jet.decel = 9.0
        jet.v = SPEEDS[-1][1] * KT
        jet.go(Leg(w.path(af.D, af.rwy(frame.distance_m(af.thr, af.D) + 130)), 90,
                   end_speed_kt=0, status="ground", name="comes_to_rest"))
        await sim.sleep(8)
        result["ok"] = False
        sim.tick_hooks.remove(follow)
        sim.tick_hooks.remove(watch)
        return result

    # ---- the truck is not on the runway: the jet rolls through and vacates
    jet.status = "ground"
    jet.v = SPEEDS[-1][1] * KT
    jet.go(Leg(w.path(af.D, af.rwy(1500)), 90, end_speed_kt=14, status="ground", name="rollout"),
           Leg(w.path(af.rwy(1500), exit_pt), 14, name="vacate"),
           Leg(w.path(exit_pt, af.b_abeam(af.stand(1)), af.stand(1)), 15, end_speed_kt=0,
               name="taxi_in"))
    if truck.held:
        result["truck_stopped_m_from_runway"] = round(edge_s - truck.s, 1)
        await sim.say(TWR, "TRUCK1", HOLDING)
    await sim.until(lambda: jet.finished("vacate"))
    await sim.say(TWR, "JZA646", JET_CLEAR)
    if truck.held:
        # the vehicles still have an emergency to reach: a controller-approved instruction
        # from Hold Short, or failing that the controller himself, sends them across
        approved = await sim.until(
            lambda: any(c.get("action") != "stop" and "cross" in (c.get("text") or "").lower()
                        for c in truck.calls),
            timeout=float(opts.get("resume_wait_s", 20)))
        if not approved:
            await sim.atc(TWR, CLEARANCE)
        await sim.say(TWR, "TRUCK1", RESUME_READBACK)
        truck.release()
        if truck.idle:
            truck.go(Leg(w.path(truck.position, af.D, af.HP_D_SE), TRUCK_TOP_KT, name="cross"),
                     Leg(w.path(af.HP_D_SE, af.D_B, af.b_abeam(spot), spot), 20,
                         end_speed_kt=0, name="to_terminal"))
        for v in convoy[1:]:
            if v.idle:
                v.go(Leg(w.path(v.position, af.D, af.HP_D_SE), TRUCK_TOP_KT, name="cross"),
                     Leg(w.path(af.HP_D_SE, af.D_B, af.b_abeam(spot), spot), 20,
                         end_speed_kt=0, name="to_terminal"))
        await sim.until(lambda: truck.finished("cross"))
        await sim.say(TWR, "TRUCK1", CLEAR)
        result["outcome"] = "held_short_then_crossed_behind_the_landing"
    if not opts.get("short", False):
        await sim.until(lambda: convoy[-1].finished("cross") or not truck.finished("cross"),
                        timeout=90)
        await sim.until(lambda: jet.finished("taxi_in"), timeout=120)
    sim.tick_hooks.remove(follow)
    sim.tick_hooks.remove(watch)
    result["ok"] = result["outcome"].startswith("held_short")
    return result
