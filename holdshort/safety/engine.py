"""Safety rule engine: pure functions over radio calls, no database.

Usage (the safety agent's loop, per transmission, in order)::

    intents = evaluate(tx, state, active_rule_set)   # check against state BEFORE tx
    state = apply_transmission(state, tx)            # then fold tx into state

``evaluate`` looks at a new clearance and the clearances already active on the
same runway. It only ever emits ``stop`` intents from agent ``safety``: the AI
can stop traffic, never clear it.

Active clearances (``AirfieldState``) are tracked per runway:

* a ``clearance`` transmission with instruction land / takeoff / cross /
  line_up_wait on runway R makes the addressed callsign active on R (replacing
  any earlier clearance that callsign had);
* any other instruction sent to that callsign (hold_short, go_around, taxi,
  a ``stop`` call, ...) ends its runway clearances;
* a call *from* the callsign reporting the runway clear ("clear of runway 04",
  "vacated", "airborne") ends its clearance on that runway.

Unverified transmissions are still evaluated: a false stop is safe, a missed
one is not. The action gate decides whether the intent executes.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from datetime import datetime
from types import MappingProxyType
from typing import Literal, get_args

from holdshort.contracts import Intent, IntentAction, RuleSet, Transmission

RULE_NO_CROSSING = "no_crossing_during_active_landing"
RULE_NO_LANDING_OCCUPIED = "no_landing_on_occupied_runway"
RULE_LOW_VIS = "low_vis_one_movement"

RunwayInstruction = Literal["land", "takeoff", "cross", "line_up_wait"]
RUNWAY_INSTRUCTIONS: frozenset[str] = frozenset(get_args(RunwayInstruction))
"""Instructions that put a callsign on (or onto) a runway."""

USING_RUNWAY: frozenset[str] = frozenset({"land", "takeoff"})
"""A landing or departing aircraft is using the runway at speed."""

OCCUPYING_RUNWAY: frozenset[str] = frozenset({"cross", "line_up_wait", "takeoff"})
"""Clearances that put something on the runway a landing aircraft would hit."""

_ALLOWED_ACTIONS: frozenset[str] = frozenset(get_args(IntentAction))
_STOP: IntentAction = "stop"
assert _STOP in _ALLOWED_ACTIONS and "clear" not in _ALLOWED_ACTIONS

_REPORT_CLEAR = re.compile(r"\b(clear of|runway clear|vacated|airborne)\b", re.IGNORECASE)

_PHRASE = {
    "land": "cleared to land",
    "takeoff": "cleared for takeoff",
    "cross": "cleared to cross",
    "line_up_wait": "cleared to line up and wait",
}


@dataclass(frozen=True)
class Clearance:
    """One active runway clearance: who, what, since when."""

    callsign: str
    instruction: RunwayInstruction
    runway: str
    since: datetime
    at: str | None = None

    def describe(self) -> str:
        """E.g. "AC8646 cleared to land" (used as Intent.conflicts_with)."""
        return f"{self.callsign} {_PHRASE[self.instruction]}"


@dataclass(frozen=True)
class AirfieldState:
    """Active clearances per runway. Immutable: ``apply_transmission`` returns a new one."""

    runways: MappingProxyType[str, tuple[Clearance, ...]] = field(
        default_factory=lambda: MappingProxyType({})
    )

    def active(self, runway: str) -> tuple[Clearance, ...]:
        """Clearances currently active on ``runway`` (oldest first)."""
        return self.runways.get(runway, ())

    def clearances_of(self, callsign: str) -> tuple[Clearance, ...]:
        return tuple(c for cs in self.runways.values() for c in cs if c.callsign == callsign)

    def _with(self, runways: dict[str, tuple[Clearance, ...]]) -> AirfieldState:
        return replace(self, runways=MappingProxyType({r: cs for r, cs in runways.items() if cs}))


# ------------------------------------------------------------------- state update


def _is_runway_clearance(tx: Transmission) -> bool:
    return tx.kind == "clearance" and tx.instruction in RUNWAY_INSTRUCTIONS and bool(tx.runway)


def _drop(
    runways: dict[str, tuple[Clearance, ...]], callsign: str, runway: str | None = None
) -> None:
    for r in list(runways):
        if runway is None or r == runway:
            runways[r] = tuple(c for c in runways[r] if c.callsign != callsign)


def apply_transmission(state: AirfieldState, tx: Transmission) -> AirfieldState:
    """Return the airfield state after ``tx``. Never mutates ``state``."""
    runways = dict(state.runways)

    if _is_runway_clearance(tx):
        assert tx.runway is not None
        _drop(runways, tx.to)  # a new clearance replaces whatever the callsign had
        new = Clearance(tx.to, tx.instruction, tx.runway, tx.t, tx.at)  # type: ignore[arg-type]
        runways[tx.runway] = (*runways.get(tx.runway, ()), new)
        return state._with(runways)

    # Any other instruction to a callsign (hold short, go around, taxi, stop) ends
    # its runway clearances. Readbacks and requests do not change anything here.
    if tx.kind == "stop" or (tx.kind == "clearance" and tx.instruction != "none"):
        _drop(runways, tx.to)
        return state._with(runways)

    # The callsign itself reports the runway clear / vacated / airborne.
    if tx.kind != "clearance" and _REPORT_CLEAR.search(tx.transcript):
        _drop(runways, tx.from_, tx.runway)
        return state._with(runways)

    return state


# ----------------------------------------------------------------------- rules


def _stop(tx: Transmission, rule: str, target: list[str], conflict: Clearance) -> Intent:
    intent = Intent(
        t=tx.t,
        agent="safety",
        action=_STOP,
        target=target,
        runway=tx.runway,
        reason=rule,
        conflicts_with=conflict.describe(),
    )
    if intent.action not in _ALLOWED_ACTIONS or intent.action == "clear":  # belt and braces
        raise AssertionError(f"safety engine produced forbidden action {intent.action!r}")
    return intent


def _max_movements(rules: RuleSet) -> int:
    for r in rules.rules:
        if r.id == RULE_LOW_VIS:
            return int(r.params.get("max_movements_per_runway", 1))
    return 1


def evaluate(tx: Transmission, state: AirfieldState, rules: RuleSet) -> list[Intent]:
    """Check ``tx`` against the clearances active in ``state`` (state before ``tx``).

    Returns zero or more ``stop`` intents, at most one per conflicting callsign.
    Target order is always [the mover on the runway, the landing/departing aircraft].
    """
    if not _is_runway_clearance(tx):
        return []
    assert tx.runway is not None
    enabled = {r.id for r in rules.rules}
    others = [c for c in state.active(tx.runway) if c.callsign != tx.to]
    intents: list[Intent] = []
    flagged: set[str] = set()

    for c in others:
        if (
            RULE_NO_CROSSING in enabled
            and tx.instruction == "cross"
            and c.instruction in USING_RUNWAY
        ):
            intents.append(_stop(tx, RULE_NO_CROSSING, [tx.to, c.callsign], c))
            flagged.add(c.callsign)
        elif (
            RULE_NO_LANDING_OCCUPIED in enabled
            and tx.instruction == "land"
            and c.instruction in OCCUPYING_RUNWAY
        ):
            intents.append(_stop(tx, RULE_NO_LANDING_OCCUPIED, [c.callsign, tx.to], c))
            flagged.add(c.callsign)

    if RULE_LOW_VIS in enabled and len(others) >= _max_movements(rules):
        for c in others:
            if c.callsign not in flagged:
                intents.append(_stop(tx, RULE_LOW_VIS, [tx.to, c.callsign], c))
                flagged.add(c.callsign)

    return intents
