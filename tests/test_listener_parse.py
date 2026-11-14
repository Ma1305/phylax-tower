"""Listener parse() normalisation and readback rules (fake LLM, no network or database)."""

from datetime import UTC, datetime

import pytest

from holdshort.listener import FLAG_STOP_NO_CALLSIGN, FLAG_UNCLEAR, Listener, parse
from holdshort.listener.normalize import normalize_callsign, normalize_runway, normalize_taxiway

T = datetime(2026, 3, 22, 23, 37, 18, tzinfo=UTC)


def fake(**label):
    base = {"intelligible": True, "from": "TOWER", "to": "UNKNOWN", "kind": "other",
            "instruction": "none", "runway": None, "at": None}
    base.update(label)
    return lambda text, ctx: dict(base)


@pytest.mark.parametrize("spoken, expected", [
    ("Truck 1 and company", "TRUCK1"),
    ("Truck one and company", "TRUCK1"),
    ("truck one", "TRUCK1"),
    ("the jet", "AC8646"),
    ("Air Canada eight six four six", "AC8646"),
    ("Air Canada 8646", "AC8646"),
    ("AC8646", "AC8646"),
    ("LaGuardia tower", "TOWER"),
    ("Tower", "TOWER"),
    ("", "UNKNOWN"),
    (None, "UNKNOWN"),
    ("all stations", "UNKNOWN"),
])
def test_normalize_callsign(spoken, expected):
    assert normalize_callsign(spoken) == expected


@pytest.mark.parametrize("spoken, expected", [
    ("4", "04"), ("four", "04"), ("runway 4", "04"), ("04", "04"), ("04L", "04"),
    ("22", "22"), ("two two", "22"), ("one three", "13"), (None, None), ("", None),
    ("forty", None), ("99", None),
])
def test_normalize_runway(spoken, expected):
    assert normalize_runway(spoken) == expected


@pytest.mark.parametrize("spoken, expected", [
    ("Delta", "D"), ("at Delta", "D"), ("D", "D"), ("taxiway delta", "D"), (None, None),
])
def test_normalize_taxiway(spoken, expected):
    assert normalize_taxiway(spoken) == expected


def test_parse_normalises_truck_crossing_clearance():
    tx = parse("Truck one and company, cross runway four at Delta.", {"t": T},
               confidence=0.95,
               llm=fake(to="Truck 1 and company", kind="clearance", instruction="cross",
                        runway="4", at="Delta"))
    assert (tx.from_, tx.to, tx.kind, tx.instruction, tx.runway, tx.at) == (
        "TOWER", "TRUCK1", "clearance", "cross", "04", "D")
    assert tx.verified and tx.flag is None


def test_parse_resolves_the_jet():
    tx = parse("The jet, go around.", {"t": T}, confidence=0.9,
               llm=fake(to="the jet", kind="clearance", instruction="go_around"))
    assert tx.to == "AC8646"


def test_speaker_hint_wins_over_llm_sender():
    tx = parse("Cleared to land runway four, Air Canada eight six four six.",
               {"t": T, "speaker": "AC8646"}, confidence=0.95,
               llm=fake(**{"from": "TOWER"}, to="TOWER", kind="readback",
                        instruction="land", runway="four"))
    assert tx.from_ == "AC8646" and tx.runway == "04"


def test_low_confidence_is_flagged_never_guessed():
    text = "Tower, Truck one, uh, we're at, uh... Delta"
    tx = parse(text, {"t": T}, confidence=0.55,
               llm=fake(**{"from": "TRUCK1"}, to="TOWER", kind="request",
                        instruction="cross", runway="4", at="Delta"))
    assert (tx.kind, tx.instruction, tx.runway, tx.at) == ("other", "none", None, None)
    assert tx.verified is False and tx.flag == FLAG_UNCLEAR and tx.transcript == text


def test_unparseable_is_flagged():
    tx = parse("kshhh ... runway ... kshh", {"t": T}, confidence=0.95, llm=lambda t, c: None)
    assert tx.kind == "other" and not tx.verified and tx.flag == FLAG_UNCLEAR


def test_unintelligible_label_is_flagged():
    tx = parse("say again?", {"t": T}, confidence=0.95, llm=fake(intelligible=False))
    assert tx.flag == FLAG_UNCLEAR and not tx.verified


def test_stop_without_callsign_goes_to_unknown():
    tx = parse("Stop, stop, stop!", {"t": T}, confidence=0.95,
               llm=fake(to="TRUCK1", kind="stop", instruction="hold_short", runway="4"))
    assert (tx.kind, tx.to, tx.instruction, tx.runway) == ("stop", "UNKNOWN", "none", None)
    assert tx.flag == FLAG_STOP_NO_CALLSIGN


def test_stop_with_named_callsign_keeps_it():
    tx = parse("Truck one, stop, stop, stop!", {"t": T}, confidence=0.95,
               llm=fake(to="TRUCK1", kind="stop"))
    assert tx.to == "TRUCK1" and tx.flag is None


def test_readback_matched_mismatch_missing():
    labels = iter([
        dict(to="TRUCK1", kind="clearance", instruction="cross", runway="4", at="Delta"),
        dict(**{"from": "TRUCK1"}, to="TOWER", kind="readback", instruction="cross",
             runway="4", at="Delta"),
        dict(**{"from": "TRUCK1"}, to="TOWER", kind="readback", instruction="cross",
             runway="22", at="Delta"),
    ])

    def llm(text, ctx):
        return {"intelligible": True, "from": "TOWER", **next(labels)}

    lst = Listener(llm=llm)
    clr = lst.handle_text("x", t=T, confidence=0.95)
    ok = lst.handle_text("x", t=T, confidence=0.95)
    bad = lst.handle_text("x", t=T, confidence=0.95)
    assert (clr.readback, ok.readback, bad.readback) == ("missing", "matched", "mismatch")


def test_readback_without_clearance_is_mismatch():
    lst = Listener(llm=fake(**{"from": "AC8646"}, to="TOWER", kind="readback",
                            instruction="land", runway="4"))
    assert lst.handle_text("x", t=T, confidence=0.95).readback == "mismatch"


def test_offline_labeller_matches_every_script_expect():
    """Text mode without an LLM key: every script line except the unclear one matches."""
    from holdshort.listener.__main__ import check
    from holdshort.listener.agent import load_script, script_time, text_confidence
    from holdshort.listener.rules import label

    start, lines = load_script()
    lst = Listener(llm=label)
    results = [(e, lst.handle_text(e["text"], t=script_time(start, e),
                                   confidence=text_confidence(e), speaker=e["speaker"]))
               for e in lines]
    assert check(results) == []
    unclear = [tx for e, tx in results if e.get("unclear")]
    assert unclear and all(tx.flag == FLAG_UNCLEAR and not tx.verified for tx in unclear)
