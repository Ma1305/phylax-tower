"""Pure state logic: clearances, hold_short and lane_status. No database in here.

``StateEngine`` holds the live picture in memory (objects, callsign-only reservations,
last lane_status per lane) and every ``on_*`` / ``tick`` call returns the writes the
worker must apply: ``Writes(objects={object_id: {clearance, hold_short}},
lanes={lane_id: full lane_status doc})``. Lane documents are only returned when they
really changed (``updated_at`` aside), and at most once per ``flush``.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta

RESERVING = ("land", "takeoff", "cross", "line_up_wait")
ENDING = ("hold_short", "stop", "go_around", "taxi")
SETS_HOLD_SHORT = ("hold_short", "stop")
CONFLICTING = ("land", "takeoff")
LEAVE_GRACE = timedelta(seconds=30)
CALLSIGN_HOLD = timedelta(minutes=5)
RUNWAY_ALT_FT = 200
VACATED_RE = re.compile(
    r"\b(clear(ed)? of (the )?(runway|rwy|\d{1,2})|vacated|vacating|off the runway|airborne)\b",
    re.I,
)
STATE_RANK = {"closed": 4, "conflict": 3, "occupied": 2, "reserved": 1, "clear": 0}


# ------------------------------------------------------------------ pure helpers


def clearance_from(tx: dict, lane_id: str | None) -> dict:
    return {"instruction": tx["instruction"], "lane_id": lane_id, "at": tx.get("at"),
            "since": tx["t"], "transmission_id": tx.get("_id")}


def transmission_effect(tx: dict) -> tuple[str, str, bool | None] | None:
    """What a transmission does to its target's clearance.

    Returns (callsign, action, hold_short) with action "set" | "end" | "end_cross", or None.
    hold_short None = leave it as it is.
    """
    kind, instr, direction = tx.get("kind"), tx.get("instruction"), tx.get("direction")
    if direction == "out" and kind == "stop":
        return (tx.get("to"), "end_cross", True)
    if kind == "report":
        if VACATED_RE.search(tx.get("transcript") or ""):
            return (tx.get("from"), "end", None)
        return None
    if direction != "in":
        return None
    if kind == "clearance" and instr in RESERVING:
        return (tx.get("to"), "set", False)
    if kind == "stop" or (kind in ("clearance", "other") and instr in ENDING):
        return (tx.get("to"), "end", True if (instr in SETS_HOLD_SHORT or kind == "stop")
                else None)
    return None


def on_lane(obj: dict, lane_id: str, lane_kind: str | None) -> bool:
    """An object is on a lane when its lane_id is that lane; on a runway it must also be
    on the ground or airborne below 200 ft."""
    if obj.get("lane_id") != lane_id:
        return False
    if lane_kind == "runway" and obj.get("status") == "airborne":
        alt = obj.get("alt_ft")
        return alt is not None and alt < RUNWAY_ALT_FT
    return True


def lane_state(occupants: list[str], reservations: list[dict]) -> tuple[str, str | None]:
    """(state, reason) by the priority rules: conflict > occupied > reserved > clear."""
    for r in reservations:
        if r["instruction"] not in CONFLICTING:
            continue
        others_on = [o for o in occupants if o != r.get("object_id")]
        others_res = [x for x in reservations if x is not r]
        if others_on or others_res:
            who = [*others_on, *(x["callsign"] for x in others_res)]
            return "conflict", (f"{r['callsign']} cleared to {r['instruction']} with "
                                f"{', '.join(dict.fromkeys(who))} on or cleared for the lane")
    if occupants:
        return "occupied", None
    if reservations:
        return "reserved", None
    return "clear", None


def build_lane_status(lane: dict, owner: str | None, objects: dict[str, dict],
                      pending: dict[str, dict], now: datetime) -> dict:
    lane_id = lane["_id"]
    occupants = sorted(oid for oid, o in objects.items()
                       if on_lane(o, lane_id, lane.get("kind")))
    res = []
    for oid, o in objects.items():
        cl = o.get("clearance")
        if cl and cl.get("lane_id") == lane_id and cl.get("instruction") in RESERVING:
            res.append({"object_id": oid, "callsign": o.get("callsign") or oid,
                        "instruction": cl["instruction"], "since": cl["since"],
                        "transmission_id": cl.get("transmission_id")})
    for cs, cl in pending.items():
        if cl.get("lane_id") == lane_id:
            res.append({"object_id": None, "callsign": cs, "instruction": cl["instruction"],
                        "since": cl["since"], "transmission_id": cl.get("transmission_id")})
    res.sort(key=lambda r: (r["since"], r["callsign"]))
    state, reason = lane_state(occupants, res)
    return {"_id": lane_id, "tower_id": lane.get("tower_id"), "controller_id": owner or "",
            "state": state, "occupants": occupants, "reservations": res, "reason": reason,
            "updated_at": now}


def same_status(a: dict | None, b: dict | None) -> bool:
    if a is None or b is None:
        return a is b
    keys = ("controller_id", "state", "occupants", "reservations", "reason")
    return all(a.get(k) == b.get(k) for k in keys)


# ------------------------------------------------------------------------ engine


@dataclass
class Writes:
    objects: dict[str, dict] = field(default_factory=dict)  # id -> {clearance, hold_short}
    lanes: dict[str, dict] = field(default_factory=dict)    # id -> full lane_status

    def __bool__(self) -> bool:
        return bool(self.objects or self.lanes)


@dataclass
class _Track:
    entered: bool = False
    left_at: datetime | None = None


class StateEngine:
    """The live picture. ``lane_resolver(tx)`` maps a transmission to its lane id."""

    def __init__(self, lanes: list[dict], owners: dict[str, str | None],
                 lane_resolver: Callable[[dict], str | None]):
        self.lanes = {lane["_id"]: lane for lane in lanes}
        self.owners = owners
        self.resolve = lane_resolver
        self.objects: dict[str, dict] = {}
        self.pending: dict[str, dict] = {}        # callsign -> clearance (no object yet)
        self.tracks: dict[str, _Track] = {}
        self.status: dict[str, dict] = {}         # last known lane_status per lane
        self._dirty: set[str] = set()
        self._obj_writes: dict[str, dict] = {}

    # ----------------------------------------------------------------- inputs

    def load_status(self, doc: dict) -> None:
        """A lane_status document as stored (from start-up or the gate closing/reopening)."""
        lane_id = doc["_id"]
        prev = self.status.get(lane_id)
        closed_before = prev is not None and prev.get("state") == "closed"
        if prev is None or doc.get("state") == "closed" or closed_before:
            # first sight, closed by the gate, or reopened; echoes of our own writes
            # are ignored so a late echo never causes a second write
            self.status[lane_id] = doc
            self._dirty.add(lane_id)

    def _lanes_of(self, obj: dict | None) -> set[str]:
        if not obj:
            return set()
        out = {obj.get("lane_id")}
        cl = obj.get("clearance")
        if cl:
            out.add(cl.get("lane_id"))
        return {x for x in out if x in self.lanes}

    def _set_clearance(self, oid: str, clearance: dict | None, hold_short: bool | None):
        obj = self.objects[oid]
        self._dirty |= self._lanes_of(obj)
        obj["clearance"] = clearance
        if hold_short is not None:
            obj["hold_short"] = hold_short
        self.tracks.pop(oid, None)
        self._dirty |= self._lanes_of(obj)
        self._obj_writes[oid] = {"clearance": clearance,
                                 "hold_short": bool(obj.get("hold_short"))}

    def _find(self, callsign: str | None) -> str | None:
        if not callsign:
            return None
        if callsign in self.objects:
            return callsign
        return next((oid for oid, o in self.objects.items() if o.get("callsign") == callsign),
                    None)

    def on_object(self, doc: dict, now: datetime) -> None:
        oid = doc["_id"]
        prev = self.objects.get(oid)
        self._dirty |= self._lanes_of(prev)
        obj = dict(doc)
        if prev is not None:   # the engine is the authority on clearance / hold_short
            obj["clearance"] = prev.get("clearance")
            obj["hold_short"] = prev.get("hold_short", False)
        self.objects[oid] = obj
        self._dirty |= self._lanes_of(obj)
        cs = obj.get("callsign") or oid
        if cs in self.pending:
            self._set_clearance(oid, self.pending.pop(cs), False)
        self._track(oid, now)

    def on_object_removed(self, oid: str) -> None:
        obj = self.objects.pop(oid, None)
        self.tracks.pop(oid, None)
        self._obj_writes.pop(oid, None)
        self._dirty |= self._lanes_of(obj)

    def on_transmission(self, tx: dict, now: datetime) -> None:
        eff = transmission_effect(tx)
        if not eff or not eff[0]:
            return
        callsign, action, hold = eff
        oid = self._find(callsign)
        if action == "set":
            cl = clearance_from(tx, self.resolve(tx))
            if oid:
                self._set_clearance(oid, cl, hold)
            else:
                self.pending[callsign] = cl
                if cl["lane_id"] in self.lanes:
                    self._dirty.add(cl["lane_id"])
            return
        pend = self.pending.get(callsign)
        if pend and (action == "end" or pend["instruction"] == "cross"):
            self.pending.pop(callsign)
            self._dirty.add(pend.get("lane_id"))
        if not oid:
            return
        cl = self.objects[oid].get("clearance")
        if action == "end_cross" and not (cl and cl.get("instruction") == "cross"):
            if self.objects[oid].get("hold_short") != hold:
                self._set_clearance(oid, cl, hold)
            return
        self._set_clearance(oid, None, hold)

    # ------------------------------------------------------------------ timers

    def _track(self, oid: str, now: datetime) -> None:
        obj = self.objects[oid]
        cl = obj.get("clearance")
        if not cl or cl.get("instruction") not in RESERVING or not cl.get("lane_id"):
            return
        lane_id = cl["lane_id"]
        tr = self.tracks.setdefault(oid, _Track())
        if on_lane(obj, lane_id, self.lanes.get(lane_id, {}).get("kind")):
            tr.entered, tr.left_at = True, None
        elif tr.entered and tr.left_at is None:
            tr.left_at = now

    def tick(self, now: datetime) -> None:
        for oid, tr in list(self.tracks.items()):
            if tr.left_at is not None and now - tr.left_at >= LEAVE_GRACE and oid in self.objects:
                self._set_clearance(oid, None, None)
        for cs, cl in list(self.pending.items()):
            if now - cl["since"] >= CALLSIGN_HOLD:
                self.pending.pop(cs)
                self._dirty.add(cl.get("lane_id"))

    # ------------------------------------------------------------------ output

    def recompute_all(self) -> None:
        self._dirty |= set(self.lanes)

    def flush(self, now: datetime) -> Writes:
        """Run timers, then return every object write and each lane that really changed."""
        self.tick(now)
        out = Writes(objects=self._obj_writes)
        self._obj_writes = {}
        dirty, self._dirty = self._dirty & set(self.lanes), set()
        for lane_id in sorted(dirty):
            prev = self.status.get(lane_id)
            if prev is not None and prev.get("state") == "closed":
                continue                               # the gate owns a closed lane
            doc = build_lane_status(self.lanes[lane_id], self.owners.get(lane_id),
                                    self.objects, self.pending, now)
            if same_status(prev, doc):
                continue
            self.status[lane_id] = doc
            out.lanes[lane_id] = doc
        return out
