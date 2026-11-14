"""Callsign resolution against live traffic (model stubbed)."""

from holdshort.listener import rules
from holdshort.listener.parse import hybrid_label, parse, rules_complete

LIVE = {"JZA646": "aircraft", "TRUCK1": "vehicle", "TRUCK7": "vehicle",
        "FFT4195": "aircraft", "UAL2384": "aircraft"}
CTX = {"live": LIVE, "station": "TOWER",
       "fields": {"tower_id": "KLGA", "stream_id": "twr", "controller_id": "c1"}}


def _p(text, ctx=CTX, llm=None):
    return parse(text, dict(ctx), llm=llm or rules.label)


def test_jazz_landing_clearance_resolves():
    tx = _p("Jazz six four sic, LaGuardia Tower, runway four, cleared to land.")
    assert (tx.from_, tx.to, tx.kind, tx.instruction, tx.runway) == (
        "TOWER", "JZA646", "clearance", "land", "04")


def test_frontier_stop():
    tx = _p("Frontier four one niner five, just stop there please.")
    assert (tx.kind, tx.to) == ("stop", "FFT4195")


def test_united_digits_and_words():
    for text in ("United 2384, ground, say your intentions.",
                 "United two three eight four, ground, say your intentions."):
        assert rules.label(text, CTX)["to"] == "UAL2384"


def test_truck_not_glued_to_frontier():
    lab = rules.label("Truck seven four one niner five on bravo", CTX)
    assert not lab["unresolved"]
    from holdshort.listener import callsigns
    got = [c for _, _, c in callsigns.find("truck seven four one niner five on bravo", LIVE)]
    assert got == ["TRUCK7", "FFT4195"]


def test_struck_is_truck_one():
    tx = _p("Struck one and company, cross runway four at Delta.")
    assert (tx.to, tx.kind, tx.instruction, tx.at) == ("TRUCK1", "clearance", "cross", "D")


def test_truck_crossing_and_stop_anywhere():
    assert _p("Truck one and company, cross runway four at Delta.").to == "TRUCK1"
    assert _p("Stop. Truck one, stop").to == "TRUCK1"
    assert _p("Stop, Truck one. Stop!").to == "TRUCK1"
    stop = _p("Frontier four one niner five, just stop there please. Stop, stop, stop, "
              "stop. Truck one, stop, stop, stop.")
    assert stop.kind == "stop" and stop.to in ("FFT4195", "TRUCK1")


def test_stop_naming_nobody_is_complete():
    lab = rules.label("Stop, stop, stop", {**CTX, "speaker": "TOWER"})
    assert rules_complete(lab) and lab["to"] == "UNKNOWN"


def test_one_wrong_digit_unique_only():
    assert rules.label("Jazz six four seven, cleared to land runway four", CTX)["to"] \
        == "JZA646"
    live = {**LIVE, "JZA647": "aircraft"}
    lab = rules.label("Jazz six four eight, cleared to land runway four", {**CTX, "live": live})
    assert lab["unresolved"]


def test_unresolved_asks_model(monkeypatch):
    calls = []

    def fake(text, ctx):
        calls.append(ctx)
        return {"intelligible": True, "from": "TOWER", "to": "JZA646", "kind": "clearance",
                "instruction": "land", "runway": "4", "at": None}
    import sys
    monkeypatch.setattr(sys.modules["holdshort.listener.parse"], "llm_label", fake)
    out = hybrid_label("Jazz blah blah, cleared to land runway four", CTX)
    assert out["to"] == "JZA646" and calls
    calls.clear()
    out = hybrid_label("Jazz six four six, LaGuardia Tower, runway four, cleared to land.", CTX)
    assert out["to"] == "JZA646" and not calls  # rules were enough


def test_prompt_lists_live_traffic():
    from holdshort.listener.parse import _user_prompt
    assert "JZA646 (aircraft)" in _user_prompt("x", CTX)


def test_air_canada_still_works():
    assert rules.label("Air Canada eight six four six, cleared to land runway four",
                       {"live": {"AC8646": "aircraft"}})["to"] == "AC8646"
    assert rules.label("Air Canada eight six four six, cleared to land runway four",
                       {})["to"] == "AC8646"
