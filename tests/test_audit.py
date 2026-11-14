"""Audit agent against Atlas ("<MONGODB_DB>_test") with a LaGuardia-like sequence."""

import json
import os
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from holdshort.audit import explain, timeline
from holdshort.audit.core import AGENTS_WITH_INTENTS, build_pipeline
from holdshort.contracts import Intent, Transmission, to_mongo
from holdshort.gate import decide, set_permission

T0 = datetime(2026, 3, 22, 23, 37, 0, tzinfo=UTC)


def _tx(db, sec, frm, to, kind, instr, transcript, **kw):
    doc = Transmission(t=T0 + timedelta(seconds=sec), **{"from": frm}, to=to, kind=kind,
                       instruction=instr, transcript=transcript, confidence=0.95,
                       readback=kw.pop("readback", "not_applicable"), verified=True, **kw)
    return db.transmissions.insert_one(to_mongo(doc)).inserted_id


@pytest.fixture(scope="module")
def lga(test_db):
    db = test_db
    for coll in ["transmissions", "decisions", *[f"intents_{a}" for a in AGENTS_WITH_INTENTS]]:
        db[coll].delete_many({})
    for a in ("safety", "procedures", "system"):
        set_permission(db, a, True, "controller_test")
    _tx(db, 0, "TOWER", "AC8646", "clearance", "land", "AC8646 runway 04 cleared to land",
        runway="04")
    _tx(db, 20, "TOWER", "TRUCK1", "clearance", "cross", "TRUCK1 cross runway 04 at D",
        runway="04", at="D")
    _tx(db, 24, "TRUCK1", "TOWER", "readback", "cross", "crossing 04 at D TRUCK1",
        runway="04", at="D", readback="matched")
    intent = to_mongo(Intent(t=T0 + timedelta(seconds=25), agent="safety", action="stop",
                             target=["TRUCK1"], runway="04",
                             reason="no_crossing_during_active_landing",
                             conflicts_with="AC8646"))
    intent["_id"] = db.intents_safety.insert_one(intent).inserted_id
    decision = decide(db, "safety", intent)
    _tx(db, 27, "TOWER", "TRUCK1", "stop", "hold_short", "TRUCK1 stop stop stop, hold short 04",
        runway="04", at="D")
    return SimpleNamespace(db=db, intent=intent, decision=decision)


def test_timeline_contains_stop_decision(lga):
    items = timeline("truck1", db=lga.db)
    kinds = [i["kind"] for i in items]
    assert kinds.count("transmission") == 3 and "intent" in kinds
    d = next(i for i in items if i["kind"] == "decision")
    assert d["doc_id"] == str(lga.decision["_id"])
    assert d["rule"] == "tier_autonomous"
    perm = lga.db.permissions.find_one({"agent": "safety"})
    assert d["authorized_by"] == {"permission_id": str(perm["_id"]),
                                  "rule_set_version": "normal_v1"}
    assert "no_crossing_during_active_landing" in d["summary"]
    assert set(d) == {"t", "kind", "actor", "summary", "rule", "authorized_by",
                      "approved_by", "doc_id"}
    ts = [i["t"] for i in items]
    assert ts == sorted(ts)
    assert timeline("TRUCK1", db=lga.db) == items  # deterministic
    # the landing clearance to AC8646 is not about TRUCK1
    assert all("cleared to land" not in i["summary"] for i in items)


def test_timeline_window(lga):
    near = timeline("TRUCK1", T0 + timedelta(seconds=25), db=lga.db, window=timedelta(seconds=2))
    assert {i["kind"] for i in near} == {"transmission", "intent", "decision"}
    assert timeline("TRUCK1", "2026-03-22T10:00:00Z", db=lga.db) == []


def test_pipeline_is_read_only():
    def keys(x):
        if isinstance(x, dict):
            for k, v in x.items():
                yield k
                yield from keys(v)
        elif isinstance(x, list):
            for v in x:
                yield from keys(v)

    assert not {"$out", "$merge"} & set(keys(build_pipeline("TRUCK1")))


def test_explain_without_llm(lga):
    out = explain("Why was TRUCK1 held?", db=lga.db, client=None)
    assert out["entity"] == "TRUCK1" and out["source"] == "deterministic"
    d = next(i for i in out["timeline"] if i["kind"] == "decision")
    assert d["rule"] == "tier_autonomous" and d["authorized_by"]["rule_set_version"]
    assert f"[{d['doc_id']}]" in out["narrative"]


class _FakeClient:
    """Stands in for anthropic.Anthropic: first call picks the entity, second narrates."""

    def __init__(self, narrative):
        self.narrative = narrative
        self.messages = self

    def create(self, **kw):
        user = json.loads(kw["messages"][0]["content"])
        if "timeline" in user:
            text = json.dumps({"narrative": self.narrative(user["timeline"])})
        else:
            text = json.dumps({"entity": "TRUCK1", "around": ""})
        return SimpleNamespace(stop_reason="end_turn",
                               content=[SimpleNamespace(type="text", text=text)])


def test_explain_llm_narrative_must_cite_timeline(lga):
    def good(tl):
        d = next(i for i in tl if i["kind"] == "decision")
        return f"Safety stopped TRUCK1 [{d['doc_id']}]."

    out = explain("Why was TRUCK1 held?", db=lga.db, client=_FakeClient(good))
    assert out["source"] == "llm" and "Safety stopped TRUCK1" in out["narrative"]

    out = explain("Why was TRUCK1 held?", db=lga.db,
                  client=_FakeClient(lambda tl: "Invented [0123456789abcdef01234567]."))
    assert out["source"] == "deterministic"
    assert "Invented" not in out["narrative"]


@pytest.mark.skipif(not os.environ.get("LLM_API_KEY"), reason="LLM_API_KEY is not set")
def test_explain_live_llm(lga):
    out = explain("Why was TRUCK1 held?", db=lga.db)
    assert out["entity"] == "TRUCK1"
    d = next(i for i in out["timeline"] if i["kind"] == "decision")
    assert d["rule"] == "tier_autonomous"
    assert d["authorized_by"]["permission_id"]
    print(out["source"], out["narrative"])
