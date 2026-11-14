"""Safety rule engine: pure, no database."""

from datetime import UTC, datetime, timedelta
from typing import get_args

import pytest
from setup_atlas import RULE_SETS

from holdshort.contracts import IntentAction, RuleSet, Transmission
from holdshort.safety.engine import AirfieldState, apply_transmission, evaluate

T0 = datetime(2026, 3, 22, 23, 35, 0, tzinfo=UTC)
NORMAL = RuleSet.model_validate(next(r for r in RULE_SETS if r["version"] == "normal_v1"))
LOW_VIS = RuleSet.model_validate(next(r for r in RULE_SETS if r["version"] == "low_vis_v1"))


def tx(sec, frm, to, kind, instruction, runway=None, text="", at=None):
    return Transmission.model_validate(
        {
            "t": T0 + timedelta(seconds=sec),
            "from": frm,
            "to": to,
            "kind": kind,
            "instruction": instruction,
            "runway": runway,
            "at": at,
            "transcript": text,
            "confidence": 0.95,
            "readback": "not_applicable",
            "verified": True,
        }
    )


def run(seq, rules):
    """Feed transmissions in order; return all intents and the final state."""
    state, intents = AirfieldState(), []
    for t in seq:
        intents += evaluate(t, state, rules)
        state = apply_transmission(state, t)
    return intents, state


LAGUARDIA = [
    tx(0, "TOWER", "AC8646", "clearance", "land", "04", "AC8646, runway 04, cleared to land."),
    tx(4, "AC8646", "TOWER", "readback", "land", "04", "Cleared to land 04, AC8646."),
    tx(20, "TOWER", "TRUCK1", "clearance", "cross", "04", "Truck 1, cross 04 at Delta.", "D"),
    tx(23, "TRUCK1", "TOWER", "readback", "cross", "04", "Crossing 04 at Delta, Truck 1.", "D"),
    tx(30, "TOWER", "JZA22", "clearance", "taxi", None, "Jazz 22, taxi to gate via Bravo."),
]


@pytest.mark.parametrize("rules", [NORMAL, LOW_VIS], ids=["normal", "low_vis"])
def test_laguardia_sequence_one_stop_truck1_and_ac8646(rules):
    intents, _ = run(LAGUARDIA, rules)
    assert len(intents) == 1
    i = intents[0]
    assert i.agent == "safety" and i.action == "stop"
    assert i.target == ["TRUCK1", "AC8646"]
    assert i.reason == "no_crossing_during_active_landing"
    assert i.conflicts_with == "AC8646 cleared to land"
    assert i.runway == "04"
    assert i.t == LAGUARDIA[2].t


def test_landing_on_occupied_runway_symmetric():
    seq = [
        tx(0, "TOWER", "TRUCK1", "clearance", "cross", "04", "Truck 1, cross 04 at Delta.", "D"),
        tx(10, "TOWER", "AC8646", "clearance", "land", "04", "Air Canada 8646, cleared to land."),
    ]
    intents, _ = run(seq, NORMAL)
    assert len(intents) == 1
    i = intents[0]
    assert i.action == "stop" and i.reason == "no_landing_on_occupied_runway"
    assert i.target == ["TRUCK1", "AC8646"]
    assert i.conflicts_with == "TRUCK1 cleared to cross"


def test_no_conflict_after_landing_aircraft_reports_clear():
    seq = [
        tx(0, "TOWER", "AC8646", "clearance", "land", "04", "Cleared to land runway 04."),
        tx(60, "AC8646", "TOWER", "other", "none", "04", "AC8646 clear of runway 04."),
        tx(70, "TOWER", "TRUCK1", "clearance", "cross", "04", "Truck 1, cross 04 at Delta.", "D"),
    ]
    intents, state = run(seq, NORMAL)
    assert intents == []
    assert [c.callsign for c in state.active("04")] == ["TRUCK1"]


def test_no_conflict_on_different_runway():
    seq = [
        tx(0, "TOWER", "AC8646", "clearance", "land", "04", "Cleared to land runway 04."),
        tx(5, "TOWER", "TRUCK1", "clearance", "cross", "13", "Truck 1, cross runway 13.", "D"),
    ]
    assert run(seq, NORMAL)[0] == []


def test_no_conflict_after_go_around_or_stop():
    for end in (
        tx(5, "TOWER", "AC8646", "clearance", "go_around", "04", "Air Canada 8646, go around."),
        tx(5, "TOWER", "TRUCK1", "stop", "hold_short", "04", "Truck 1, stop, stop, stop."),
    ):
        first = (
            tx(0, "TOWER", "AC8646", "clearance", "land", "04", "Cleared to land.")
            if end.to == "AC8646"
            else tx(0, "TOWER", "TRUCK1", "clearance", "cross", "04", "Cross runway 04.", "D")
        )
        follow = (
            tx(10, "TOWER", "TRUCK1", "clearance", "cross", "04", "Cross runway 04.", "D")
            if end.to == "AC8646"
            else tx(10, "TOWER", "AC8646", "clearance", "land", "04", "Cleared to land.")
        )
        assert run([first, end, follow], NORMAL)[0] == []


def test_readbacks_and_repeat_clearances_do_not_fire():
    seq = [
        tx(0, "TOWER", "AC8646", "clearance", "land", "04", "Cleared to land."),
        tx(3, "AC8646", "TOWER", "readback", "land", "04", "Cleared to land."),
        tx(9, "TOWER", "AC8646", "clearance", "land", "04", "Cleared to land, wind calm."),
    ]
    intents, state = run(seq, LOW_VIS)
    assert intents == []
    assert len(state.active("04")) == 1


def test_two_crossings_low_vis_stops_but_normal_does_not():
    seq = [
        tx(0, "TOWER", "TRUCK1", "clearance", "cross", "04", "Truck 1, cross runway 04.", "D"),
        tx(5, "TOWER", "TRUCK2", "clearance", "cross", "04", "Truck 2, cross runway 04.", "D"),
    ]
    assert run(seq, NORMAL)[0] == []
    intents, _ = run(seq, LOW_VIS)
    assert len(intents) == 1
    i = intents[0]
    assert i.action == "stop" and i.reason == "low_vis_one_movement"
    assert i.target == ["TRUCK2", "TRUCK1"]
    assert i.conflicts_with == "TRUCK1 cleared to cross"


def test_low_vis_clears_after_vacated():
    seq = [
        tx(0, "TOWER", "TRUCK1", "clearance", "cross", "04", "Truck 1, cross runway 04.", "D"),
        tx(30, "TRUCK1", "TOWER", "other", "none", "04", "Truck 1 vacated runway 04."),
        tx(35, "TOWER", "TRUCK2", "clearance", "cross", "04", "Truck 2, cross runway 04.", "D"),
    ]
    assert run(seq, LOW_VIS)[0] == []


def test_rule_not_in_rule_set_does_not_fire():
    empty = RuleSet(version="x", name="x", active=True, rules=[])
    assert run(LAGUARDIA, empty)[0] == []


def test_only_allowed_actions_never_clear():
    allowed = set(get_args(IntentAction))
    seqs = [
        LAGUARDIA,
        [
            tx(0, "TOWER", "TRUCK1", "clearance", "cross", "04", "", "D"),
            tx(1, "TOWER", "TRUCK2", "clearance", "line_up_wait", "04"),
            tx(2, "TOWER", "AC8646", "clearance", "land", "04"),
            tx(3, "TOWER", "JZA22", "clearance", "takeoff", "04"),
        ],
    ]
    for seq in seqs:
        for rules in (NORMAL, LOW_VIS):
            for i in run(seq, rules)[0]:
                assert i.action in allowed and i.action == "stop" and i.action != "clear"


def test_apply_transmission_is_pure():
    s0 = AirfieldState()
    s1 = apply_transmission(s0, LAGUARDIA[0])
    assert s0.active("04") == ()
    c = s1.active("04")[0]
    assert (c.callsign, c.instruction, c.since) == ("AC8646", "land", LAGUARDIA[0].t)
    with pytest.raises(TypeError):
        s1.runways["13"] = ()  # type: ignore[index]
