"""Real proof of holdshort.llm against the configured provider (Vertex AI / Gemini).

Three calls: parse a non-standard radio call the rules cannot handle, draft one advisor
proposal for a seeded stop, answer one audit question on a seeded timeline. Prints each
latency. Exit 0 = all three passed, 1 = a check failed, 2 = no provider configured.
No database needed: the stop and the timeline are seeded in memory.
"""

from __future__ import annotations

import importlib
import sys
import time
import warnings
from datetime import UTC, datetime, timedelta

warnings.filterwarnings("ignore", category=SyntaxWarning)

from holdshort import llm  # noqa: E402
from holdshort.agents import procedures_agent as pa  # noqa: E402
from holdshort.audit import core as audit_core  # noqa: E402
from holdshort.listener import rules  # noqa: E402

P = importlib.import_module("holdshort.listener.parse")


def main() -> int:
    st = llm.status()
    print("status:", st)
    if st["provider"] == "none" or not st["credentials"]:
        print("BLOCKED: no LLM provider/credentials configured")
        return 2
    ok = True

    # 1. parse
    call = ("Tower, uh, Truck one with two more behind, we'd like to get across four at "
            "Delta when able")
    fields = {"tower_id": "LGA", "stream_id": "LGA_TWR", "controller_id": "ctl_local"}
    assert not P.rules_complete(rules.label(call, {})), "rules unexpectedly handle the call"
    t0 = time.monotonic()
    tx = P.parse(call, {"fields": fields, "station": "TOWER"}, llm=P.get_labeller())
    dt = time.monotonic() - t0
    got = (tx.from_, tx.to, tx.kind, tx.instruction, tx.runway, tx.at, tx.verified)
    want = ("TRUCK1", "TOWER", "request", "cross", P.normalize_runway("4"),
            P.normalize_taxiway("Delta"), True)
    good = got == want
    ok &= good
    print(f"parse   {'PASS' if good else 'FAIL'} {dt:5.2f}s {got}")

    # 2. advisor draft for a seeded stop (TRUCK1 stopped crossing 04 at D, AC8646 landing)
    facts = {"kind": "resume", "callsign": "TRUCK1", "other": "AC8646", "runway": "04",
             "at": "D", "instruction": "cross", "other_holds_conflict": True,
             "summary": "Safety STOP: TRUCK1 was cleared to cross runway 04 at D while AC8646 "
                        "is cleared to land runway 04 and still on the runway."}
    t0 = time.monotonic()
    draft = pa.llm_drafter()(facts)
    dt = time.monotonic() - t0
    rejected = pa.guard(draft, facts)
    good = bool(draft)
    ok &= good
    print(f"advisor {'PASS' if good else 'FAIL'} {dt:5.2f}s {draft!r} "
          f"guard={'pass' if rejected is None else 'rejected: ' + rejected}")

    # 3. audit question on a seeded timeline
    t = datetime(2026, 11, 14, 18, 41, 7, tzinfo=UTC)
    items = [
        {"doc_id": "a1" * 12, "t": t, "kind": "transmission",
         "summary": "GROUND->TRUCK1 cross runway 04 at D, readback matched"},
        {"doc_id": "b2" * 12, "t": t + timedelta(seconds=2), "kind": "transmission",
         "summary": "TOWER->AC8646 cleared to land runway 04"},
        {"doc_id": "c3" * 12, "t": t + timedelta(seconds=3), "kind": "decision",
         "summary": "safety stop executed: TRUCK1, AC8646 on RWY_04_22",
         "rule": "no_crossing_during_active_landing",
         "authorized_by": {"permission_id": "safety", "rule_set_version": "normal_v1"},
         "approved_by": "autonomous"},
    ]
    t0 = time.monotonic()
    narrative = audit_core._llm_narrative("env", "Why was TRUCK1 stopped?", items)
    dt = time.monotonic() - t0
    good = bool(narrative) and "c3" * 12 in narrative
    ok &= good
    print(f"audit   {'PASS' if good else 'FAIL'} {dt:5.2f}s {narrative!r}")
    print("OK" if ok else "FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
