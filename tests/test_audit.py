"""Audit agent on a seeded LaGuardia-like sequence (own database, v2 shapes, LLM stubbed)."""

from datetime import UTC, datetime, timedelta

import pytest
from bson import ObjectId

from holdshort.audit import answer, explain, timeline
from holdshort.audit import core as audit_core
from tests.conftest import test_db_name

NOW = datetime.now(UTC).replace(microsecond=0)
T0 = NOW - timedelta(minutes=4)


def at(s: int) -> datetime:
    return T0 + timedelta(seconds=s)


def tx(s, stream, ctl, frm, to, kind, instr, text, **kw):
    return {"_id": ObjectId(), "tower_id": "LGA", "stream_id": stream, "controller_id": ctl,
            "t": at(s), "t_end": at(s + 3), "direction": "in", "from": frm, "to": to,
            "kind": kind, "instruction": instr, "transcript": text, "confidence": 0.95,
            "readback": "not_applicable", "verified": True, "flag": None,
            "synthetic": False, "decision_id": None, **kw}


@pytest.fixture(scope="module")
def seeded(client):
    import setup_atlas

    from holdshort.world import clear_cache

    name = test_db_name() + "_audit"
    client.drop_database(name)
    db = client[name]
    setup_atlas.setup(db, log=lambda *_: None)
    clear_cache()
    ids: dict[str, ObjectId] = {}

    land = tx(0, "LGA_TWR", "ctl_local", "TOWER", "AC8646", "clearance", "land",
              "AC8646 runway 04 cleared to land", runway="04", lane_id="RWY_04_22")
    taxi = tx(5, "LGA_GND", "ctl_ground", "GROUND", "TRUCK1", "clearance", "taxi",
              "TRUCK1 proceed via taxiway B", at="B", lane_id="TWY_B")
    db.transmissions.insert_many([land, taxi])
    ids["land"], ids["taxi"] = land["_id"], taxi["_id"]

    ids["lane"] = db.state_events.insert_one({
        "t": at(10), "tower_id": "LGA", "kind": "lane_status", "ref": "RWY_04_22", "by": "state",
        "doc": {"_id": "RWY_04_22", "tower_id": "LGA", "controller_id": "ctl_local",
                "state": "conflict", "occupants": ["TRUCK1"],
                "reservations": [{"object_id": "AC8646", "callsign": "AC8646",
                                  "instruction": "land", "since": at(0),
                                  "transmission_id": land["_id"]}],
                "reason": "TRUCK1 on runway 04 while AC8646 cleared to land",
                "updated_at": at(10)}}).inserted_id

    base_i = {"tower_id": "LGA", "agent": "safety", "target": ["TRUCK1"],
              "object_ids": ["TRUCK1"], "lane_id": "RWY_04_22", "runway": "04",
              "stream_id": "LGA_GND", "reason": "runway_incursion", "conflicts_with": "AC8646"}
    ids["warn_i"] = db.intents_safety.insert_one(
        {**base_i, "t": at(11), "action": "warn", "severity": "warning",
         "detail": "TRUCK1 is on runway 04 while AC8646 is cleared to land."}).inserted_id
    ids["stop_i"] = db.intents_safety.insert_one(
        {**base_i, "t": at(12), "action": "stop", "severity": "critical",
         "proposal_text": "TRUCK1 stop, hold position"}).inserted_id
    auth = {"permission_id": "safety", "rule_set_version": "normal_v1"}
    dec = {"tower_id": "LGA", "agent": "safety", "lane_id": "RWY_04_22",
           "controller_id": "ctl_local", "stream_id": "LGA_GND", "target": ["TRUCK1"],
           "mode": "proactive", "authorized_by": auth, "approved_by": None, "revokes": None,
           "notified": ["ctl_local"]}
    ids["warn_d"] = db.decisions.insert_one(
        {**dec, "t": at(11), "intent_id": ids["warn_i"], "intent_action": "warn",
         "severity": "warning", "outcome": "executed", "rule": "advisory",
         "text": "TRUCK1 on runway 04"}).inserted_id
    ids["stop_d"] = db.decisions.insert_one(
        {**dec, "t": at(12), "intent_id": ids["stop_i"], "intent_action": "stop",
         "severity": "critical", "outcome": "executed", "rule": "tier_autonomous",
         "text": "TRUCK1 stop, hold position"}).inserted_id
    out = tx(13, "LGA_GND", "ctl_ground", "HOLDSHORT", "TRUCK1", "stop", "hold_short",
             "TRUCK1 stop, hold position", direction="out", synthetic=True,
             decision_id=ids["stop_d"])
    ids["out"] = db.transmissions.insert_one(out).inserted_id

    # procedures proposes a crossing for AC1234 on runway 13; ctl_ground tries, ctl_local approves
    ids["prop_i"] = db.intents_procedures.insert_one({
        "t": at(60), "tower_id": "LGA", "agent": "procedures", "action": "propose_instruction",
        "severity": "info", "target": ["AC1234"], "object_ids": ["AC1234"],
        "lane_id": "RWY_13_31", "runway": "13", "stream_id": "LGA_TWR",
        "reason": "crossing_request", "proposal_text": "AC1234 cross runway 13 at B"}).inserted_id
    pdec = {"tower_id": "LGA", "intent_id": ids["prop_i"], "agent": "procedures",
            "intent_action": "propose_instruction", "severity": "info", "lane_id": "RWY_13_31",
            "controller_id": "ctl_local", "stream_id": "LGA_TWR", "target": ["AC1234"],
            "text": "AC1234 cross runway 13 at B", "mode": "proactive",
            "authorized_by": {"permission_id": "procedures", "rule_set_version": "normal_v1"},
            "notified": ["ctl_local"]}
    ids["pending"] = db.decisions.insert_one(
        {**pdec, "t": at(61), "outcome": "pending_approval", "rule": "needs_approval",
         "approved_by": None, "revokes": None}).inserted_id
    ids["sec"] = db.security_events.insert_one({
        "t": at(70), "controller_id": "ctl_ground", "kind": "not_lane_owner",
        "attempted": f"approve decision {ids['pending']}", "error_code": 403,
        "error": "RWY_13_31 is owned by ctl_local"}).inserted_id
    ids["approved"] = db.decisions.insert_one(
        {**pdec, "t": at(80), "outcome": "executed", "rule": "controller_approved",
         "approved_by": "ctl_local", "revokes": ids["pending"]}).inserted_id
    ids["perm"] = db.state_events.insert_one({
        "t": at(90), "tower_id": "LGA", "kind": "permission", "ref": "procedures",
        "by": "ctl_sup", "doc": {"_id": "procedures", "agent": "procedures", "enabled": False,
                                 "mode": "proactive", "tier": "needs_approval"}}).inserted_id
    yield db, {k: str(v) for k, v in ids.items()}
    client.drop_database(name)


@pytest.fixture(autouse=True)
def no_key(monkeypatch):
    monkeypatch.delenv("LLM_API_KEY", raising=False)


def test_timeline_ordered_and_complete(seeded):
    db, ids = seeded
    items = timeline(db, "LGA", around=at(45), window=timedelta(seconds=60))
    got = [it["doc_id"] for it in items]
    assert set(ids.values()) <= set(got)
    assert [it["t"] for it in items] == sorted(it["t"] for it in items)
    assert items == timeline(db, "LGA", around=at(45), window=timedelta(seconds=60))
    for it in items:
        assert set(it) == {"t", "kind", "actor", "summary", "lane_id", "controller_id", "rule",
                           "authorized_by", "approved_by", "doc_id"}
    by = {it["doc_id"]: it for it in items}
    assert by[ids["out"]]["actor"] == "HOLDSHORT"
    assert by[ids["sec"]]["kind"] == "security_event" and "403" in by[ids["sec"]]["summary"]
    assert by[ids["perm"]]["kind"] == "permission" and by[ids["perm"]]["actor"] == "ctl_sup"
    assert by[ids["lane"]]["kind"] == "lane_status"
    # same-second ties are broken by KINDS order
    assert got.index(ids["warn_i"]) < got.index(ids["warn_d"])


def test_filters(seeded):
    db, ids = seeded
    lane = {it["doc_id"] for it in timeline(db, "LGA", lane_id="RWY_13_31")}
    assert {ids["prop_i"], ids["pending"], ids["approved"], ids["perm"]} <= lane
    assert ids["stop_d"] not in lane
    ground = {it["doc_id"] for it in timeline(db, "LGA", controller_id="ctl_ground")}
    assert ids["sec"] in ground and ids["taxi"] in ground and ids["approved"] not in ground


def test_why_was_truck1_held(seeded):
    db, ids = seeded
    out = explain(db, "Why was TRUCK1 held?", llm=None)
    assert out["source"] == "rules"
    tl = {it["doc_id"]: it for it in out["timeline"]}
    stop = tl[ids["stop_d"]]
    assert stop["rule"] == "tier_autonomous"
    assert stop["authorized_by"] == {"permission_id": "safety", "rule_set_version": "normal_v1"}
    assert ids["land"] in tl  # the conflicting clearance to AC8646
    assert ids["stop_d"] in out["cited"] and ids["land"] in out["cited"]
    assert "tier_autonomous" in out["narrative"] and "safety" in out["narrative"]
    assert set(out["cited"]) <= set(tl)
    assert ids["prop_i"] not in tl


def test_who_approved_the_crossing(seeded):
    db, ids = seeded
    out = explain(db, "Who approved the crossing?", llm=None, now=at(100))
    assert "ctl_local" in out["narrative"]
    assert ids["approved"] in out["cited"]
    appr = next(it for it in out["timeline"] if it["doc_id"] == ids["approved"])
    assert appr["approved_by"] == "ctl_local"


def test_last_minutes_on_runway(seeded):
    db, ids = seeded
    out = explain(db, "What changed on runway 04 in the last five minutes?", llm=None,
                  now=at(30))
    assert out["focus"]["lane_id"] == "RWY_04_22"
    got = {it["doc_id"] for it in out["timeline"]}
    assert {ids["lane"], ids["stop_d"], ids["land"]} <= got and ids["approved"] not in got
    assert out["cited"]


class _StubStructured:
    def __init__(self, result):
        self.result, self.calls = result, []

    def invoke(self, messages):
        self.calls.append(messages)
        return self.result


class _StubLLM:
    def __init__(self, result):
        self.structured = _StubStructured(result)

    def with_structured_output(self, schema):
        self.schema = schema
        return self.structured


def test_llm_path_stubbed(seeded):
    db, ids = seeded
    good = _StubLLM({"narrative": f"TRUCK1 was stopped under tier_autonomous [{ids['stop_d']}].",
                     "cited": [ids["stop_d"]]})
    out = explain(db, "Why was TRUCK1 held?", llm=good)
    assert out["source"] == "llm" and out["cited"] == [ids["stop_d"]]
    assert good.structured.calls
    bad = _StubLLM({"narrative": "Made up [ffffffffffffffffffffffff].", "cited": []})
    out = explain(db, "Why was TRUCK1 held?", llm=bad)
    assert out["source"] == "rules" and "ffffffff" not in out["narrative"]


def test_answer_json_safe_and_read_only(seeded, monkeypatch):
    import json

    db, _ = seeded
    counts = {c: db[c].count_documents({}) for c in db.list_collection_names()
              if not c.startswith("system.")}
    assert audit_core._llm_from_env() is None
    out = answer(db, "Why was TRUCK1 held?")
    json.dumps(out)
    assert out["timeline"][0]["t"].endswith("Z")
    assert counts == {c: db[c].count_documents({}) for c in counts}
