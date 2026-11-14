"""Safety rule engine v2: pure functions over lane_status, objects and context. No I/O, no LLM.

    conflicts = lane_conflicts(lane_status, objects, lane, rule_ids)
    conflicts += readback_conflicts(context_item, rule_ids)
    for c in new_conflicts(conflicts, active_keys, lane_id): ...
    intents = intents_for(c, mode, stream_id, tower_id, t)

A ``Conflict`` is keyed by (rule, lane, sorted targets): the agent emits one warn (and in
proactive mode, for a critical conflict, one stop) per key while the conflict lasts.

Only ``warn`` and ``stop`` intents are produced. The AI never clears traffic.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, get_args

from holdshort.contracts import CALLSIGN_PATTERN, Intent, IntentAction, Severity

RULE_NO_CROSSING = "no_crossing_during_active_landing"
RULE_NO_LANDING_OCCUPIED = "no_landing_on_occupied_runway"
RULE_INCURSION = "runway_incursion"
RULE_UNCLEARED = "uncleared_entry"
RULE_READBACK = "readback_mismatch"
RULE_LOW_VIS = "low_vis_one_movement"

SEVERITY: dict[str, Severity] = {
    RULE_NO_CROSSING: "critical",
    RULE_NO_LANDING_OCCUPIED: "critical",
    RULE_INCURSION: "critical",
    RULE_UNCLEARED: "caution",
    RULE_READBACK: "warning",
    RULE_LOW_VIS: "warning",
}

LANDING = ("land", "takeoff")
RUNWAY_CLEARANCES = ("land", "takeoff", "cross", "line_up_wait")
_ALLOWED: frozenset[str] = frozenset(get_args(IntentAction))
assert "clear" not in _ALLOWED

_CALLSIGN_RE = re.compile(CALLSIGN_PATTERN)

# ------------------------------------------------------------------ spoken words

DIGITS = {"0": "zero", "1": "one", "2": "two", "3": "three", "4": "four", "5": "five",
          "6": "six", "7": "seven", "8": "eight", "9": "niner"}
TELEPHONY = {"AC": "Air Canada", "TRUCK": "Truck", "CAR": "Car", "OPS": "Ops",
             "FIRE": "Fire", "TUG": "Tug"}
KIND_WORD = {"vehicle": "Vehicle", "person": "Person", "aircraft": "Aircraft",
             "unknown": "Traffic"}


def digits_in_words(s: str) -> str:
    return " ".join(DIGITS.get(ch, ch) for ch in s)


def callsign_in_words(callsign: str) -> str:
    """'TRUCK1' -> 'Truck one', 'AC8646' -> 'Air Canada eight six four six'."""
    m = re.match(r"^([A-Z]*)([0-9]*)([A-Z0-9]*)$", callsign)
    if not m:
        return callsign
    prefix, num, rest = m.groups()
    words = []
    if prefix:
        words.append(TELEPHONY.get(prefix, prefix.capitalize() if len(prefix) > 3 else prefix))
    if num:
        words.append(digits_in_words(num))
    if rest:
        words.append(digits_in_words(rest))
    return " ".join(words)


def stop_text(callsign: str | None, kind: str | None, runway: str | None,
              on_runway: bool) -> str:
    """The exact STOP call for the party that can stop safely."""
    rwy = f"runway {digits_in_words(runway)}" if runway else "the runway"
    if not callsign:
        who = KIND_WORD.get(kind or "unknown", "Traffic")
        return f"{who} on {rwy}, STOP. Vacate the runway immediately."
    name = callsign_in_words(callsign)
    if on_runway:
        return f"{name}, STOP. Vacate {rwy} immediately."
    return f"{name}, STOP. Hold short {rwy}."


# ---------------------------------------------------------------------- conflict


@dataclass(frozen=True)
class Party:
    """One side of a conflict: an object, a reservation, or both."""

    object_id: str | None
    callsign: str | None
    kind: str | None = None
    instruction: str | None = None  # the runway clearance it holds, if any
    on_runway: bool = False

    @property
    def ident(self) -> str:
        return self.callsign or self.object_id or "?"

    def describe(self, runway: str | None) -> str:
        rwy = f"runway {runway}" if runway else "the runway"
        who = self.callsign or (f"{self.kind or 'object'} {self.object_id}")
        if self.instruction == "land":
            return f"{who} cleared to land {rwy}"
        if self.instruction == "takeoff":
            return f"{who} cleared for takeoff {rwy}"
        if self.instruction == "cross":
            return f"{who} cleared to cross {rwy}"
        if self.instruction == "line_up_wait":
            return f"{who} lined up on {rwy}"
        if self.on_runway:
            return f"{who} on {rwy}"
        return who


@dataclass(frozen=True)
class Conflict:
    rule: str
    lane_id: str | None
    runway: str | None
    parties: tuple[Party, ...]
    detail: str
    stop_party: Party | None = None
    conflicts_with: str | None = None
    extra_key: str | None = None  # e.g. the context item id for readback mismatches

    @property
    def severity(self) -> Severity:
        return SEVERITY[self.rule]

    @property
    def targets(self) -> list[str]:
        return sorted({p.callsign for p in self.parties
                       if p.callsign and _CALLSIGN_RE.match(p.callsign)})

    @property
    def object_ids(self) -> list[str]:
        return sorted({p.object_id for p in self.parties if p.object_id})

    @property
    def key(self) -> str:
        who = ",".join(self.targets or self.object_ids)
        k = f"{self.rule}|{self.lane_id or '-'}|{who}"
        return f"{k}|{self.extra_key}" if self.extra_key else k


@dataclass
class LaneView:
    """What the engine needs to know about a lane from the world."""

    id: str
    kind: str = "runway"
    designators: list[str] = field(default_factory=list)

    @property
    def runway(self) -> str | None:
        return self.designators[0] if self.kind == "runway" and self.designators else None


# ---------------------------------------------------------------------- lane rules


def _has_clearance(obj: dict | None, oid: str, lane_id: str, reservations: list[dict]) -> bool:
    cs = (obj or {}).get("callsign")
    for r in reservations:
        if r.get("instruction") in RUNWAY_CLEARANCES and (
                (r.get("object_id") and r.get("object_id") == oid)
                or (cs and r.get("callsign") == cs)):
            return True
    cl = (obj or {}).get("clearance") or {}
    return cl.get("lane_id") == lane_id and cl.get("instruction") in RUNWAY_CLEARANCES


def _same(a: Party, b: Party) -> bool:
    return bool((a.object_id and a.object_id == b.object_id)
                or (a.callsign and a.callsign == b.callsign))


def lane_conflicts(lane_status: dict[str, Any], objects: dict[str, dict], lane: LaneView,
                   rule_ids: set[str] | frozenset[str]) -> list[Conflict]:
    """Every conflict on one lane, at most one per pair of parties (most specific rule wins)."""
    if lane.kind != "runway" or lane_status.get("state") == "closed":
        return []
    lane_id = lane.id
    rwy = lane.runway
    reservations = [r for r in lane_status.get("reservations") or []
                    if r.get("instruction") in RUNWAY_CLEARANCES]
    occupant_ids = list(dict.fromkeys(lane_status.get("occupants") or []))

    by_callsign = {o.get("callsign"): oid for oid, o in objects.items() if o.get("callsign")}
    occ_set = set(occupant_ids)

    def res_party(r: dict) -> Party:
        oid = r.get("object_id") or by_callsign.get(r.get("callsign"))
        obj = objects.get(oid or "") or {}
        return Party(oid, r.get("callsign"), obj.get("kind"), r.get("instruction"),
                     on_runway=oid in occ_set)

    res_parties = [res_party(r) for r in reservations]
    occupants = []
    for oid in occupant_ids:
        obj = objects.get(oid) or {}
        occupants.append((Party(oid, obj.get("callsign"), obj.get("kind"), None, True),
                          _has_clearance(obj, oid, lane_id, reservations)))

    landers = [p for p in res_parties if p.instruction in LANDING]
    out: list[Conflict] = []
    covered: set[frozenset[str]] = set()

    def add(rule: str, a: Party, b: Party | None, detail: str, stop: Party | None,
            conflicts_with: str | None) -> None:
        pair = frozenset({a.ident, b.ident} if b else {a.ident})
        if pair in covered or rule not in rule_ids:
            return
        covered.add(pair)
        parties = (a, b) if b else (a,)
        out.append(Conflict(rule, lane_id, rwy, parties, detail, stop, conflicts_with))

    rw = f"runway {rwy}" if rwy else lane_id
    # 1. crossing while another callsign is cleared to land / take off
    for c in (p for p in res_parties if p.instruction == "cross"):
        for lnd in landers:
            if _same(c, lnd):
                continue
            add(RULE_NO_CROSSING, c, lnd,
                f"{c.ident} is cleared to cross {rw} while {lnd.describe(rwy)}; stop "
                f"{c.ident}.", c, lnd.describe(rwy))
    # 2. an uncleared occupant while another object is cleared to land / take off
    for occ, cleared in occupants:
        if cleared:
            continue
        for lnd in landers:
            if _same(occ, lnd):
                continue
            add(RULE_INCURSION, occ, lnd,
                f"{occ.describe(rwy)} without a clearance while {lnd.describe(rwy)}; stop "
                f"{occ.ident}.", occ, lnd.describe(rwy))
    # 3. a landing onto an occupied runway, or one another object is cleared onto
    for lnd in (p for p in res_parties if p.instruction == "land"):
        others = [o for o, _ in occupants] + [p for p in res_parties if p.instruction != "cross"]
        for o in others:
            if _same(o, lnd):
                continue
            stop = o if o.instruction != "land" else None
            add(RULE_NO_LANDING_OCCUPIED, o, lnd,
                f"{lnd.describe(rwy)} while {o.describe(rwy)}.", stop, lnd.describe(rwy))
    # 4. an uncleared occupant and nobody else reserved the runway
    for occ, cleared in occupants:
        if cleared or any(not _same(p, occ) for p in res_parties):
            continue
        add(RULE_UNCLEARED, occ, None,
            f"{occ.describe(rwy)} with no clearance.", None, None)
    # 5. low visibility: one movement per runway
    if RULE_LOW_VIS in rule_ids:
        movers: list[Party] = []
        for p in res_parties + [o for o, _ in occupants]:
            if not any(_same(p, m) for m in movers):
                movers.append(p)
        if len(movers) > 1:
            names = ", ".join(m.ident for m in movers)
            out.append(Conflict(RULE_LOW_VIS, lane_id, rwy, tuple(movers),
                                f"Low visibility: {len(movers)} movements on {rw} ({names}); "
                                "only one is allowed.", None, None))
    return out


def readback_conflicts(item: dict[str, Any], rule_ids: set[str] | frozenset[str]) -> list[Conflict]:
    """A context item whose readback is "mismatch" -> one readback_mismatch conflict."""
    if RULE_READBACK not in rule_ids or item.get("readback") != "mismatch":
        return []
    if item.get("superseded_by"):
        return []
    lanes = item.get("lane_ids") or []
    parties = tuple(Party(None, cs) for cs in item.get("callsigns") or []
                    if _CALLSIGN_RE.match(cs) and cs not in ("TOWER", "GROUND", "OPS"))
    who = ", ".join(p.callsign for p in parties if p.callsign) or "Traffic"
    detail = f"{who}: readback does not match the instruction ({item.get('summary', '')})."
    src = item.get("source") or {}
    return [Conflict(RULE_READBACK, lanes[0] if lanes else None, None, parties, detail,
                     extra_key=str(src.get("id") or item.get("_id")))]


# ---------------------------------------------------------------------- output


def new_conflicts(conflicts: list[Conflict], active: set[str],
                  scope_prefixes: list[str] | None = None) -> tuple[list[Conflict], set[str]]:
    """(conflicts not yet warned about, the new active key set).

    ``scope_prefixes``: key prefixes this evaluation covers (e.g. every lane rule for one
    lane); active keys under them that are not in ``conflicts`` have cleared and are dropped,
    so the conflict warns again if it returns. Keys outside the scope are kept.
    """
    now_keys = {c.key for c in conflicts}
    kept = {k for k in active
            if k in now_keys or not any(_in_scope(k, s) for s in scope_prefixes or [])}
    fresh, seen = [], set(kept)
    for c in conflicts:
        if c.key not in seen:
            fresh.append(c)
            seen.add(c.key)
    return fresh, seen


def _in_scope(key: str, scope: str) -> bool:
    rule, lane = key.split("|")[:2]
    return f"{rule}|{lane}" == scope or lane == scope


def lane_scope(lane_id: str) -> list[str]:
    """Scope prefixes for every lane rule on ``lane_id`` (used by ``new_conflicts``)."""
    return [f"{r}|{lane_id}" for r in SEVERITY if r != RULE_READBACK]


def intents_for(c: Conflict, mode: str, *, tower_id: str, t: datetime,
                stream_id: str | None = None) -> list[Intent]:
    """One warn; plus one stop when mode is proactive and the conflict is critical."""
    out = [Intent(t=t, tower_id=tower_id, agent="safety", action="warn", severity=c.severity,
                  target=c.targets, object_ids=c.object_ids, lane_id=c.lane_id,
                  runway=c.runway, stream_id=stream_id, reason=c.rule,
                  conflicts_with=c.conflicts_with, detail=c.detail)]
    sp = c.stop_party
    if mode == "proactive" and c.severity == "critical" and sp is not None:
        target = [sp.callsign] if sp.callsign and _CALLSIGN_RE.match(sp.callsign) else []
        out.append(Intent(
            t=t, tower_id=tower_id, agent="safety", action="stop", severity="critical",
            target=target, object_ids=[sp.object_id] if sp.object_id else [],
            lane_id=c.lane_id, runway=c.runway, stream_id=stream_id, reason=c.rule,
            conflicts_with=c.conflicts_with, detail=c.detail,
            proposal_text=stop_text(sp.callsign, sp.kind, c.runway, sp.on_runway)))
    for i in out:
        if i.action not in _ALLOWED or i.action == "clear":
            raise AssertionError(f"safety engine produced forbidden action {i.action!r}")
    return out
