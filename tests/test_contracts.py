"""Contracts: every model round-trips through to_mongo; the enums match CONTRACTS.md."""

from datetime import UTC, datetime, timedelta
from typing import get_args

import pytest
from bson import ObjectId
from pydantic import ValidationError

from holdshort import contracts as c
from holdshort.world import lane_area, load_world_file

NOW = datetime.now(UTC)
PT = {"type": "Point", "coordinates": [-73.87, 40.77]}
OID = ObjectId()


def samples() -> list[tuple[type, dict]]:
    w = load_world_file("LGA")
    lane = dict(w["lanes"][0], tower_id="LGA")
    lane["area"] = lane_area(lane["centerline"], lane["width_m"])
    return [
        (c.Tower, w["tower"]),
        (c.Lane, lane),
        (c.HoldPoint, dict(w["hold_points"][0], tower_id="LGA")),
        (c.Controller, dict(w["controllers"][0], tower_id="LGA")),
        (c.Stream, dict(w["streams"][0], tower_id="LGA")),
        (c.TrackedObject, {"_id": "AC8646", "tower_id": "LGA", "kind": "aircraft",
                           "callsign": "AC8646", "status": "ground", "position": PT,
                           "clearance": {"instruction": "land", "lane_id": "RWY_04_22",
                                         "since": NOW, "transmission_id": OID},
                           "updated_at": NOW, "expires_at": NOW + timedelta(seconds=120)}),
        (c.LaneStatus, {"_id": "RWY_04_22", "tower_id": "LGA", "controller_id": "ctl_local",
                        "state": "reserved", "occupants": [],
                        "reservations": [{"object_id": "AC8646", "callsign": "AC8646",
                                          "instruction": "land", "since": NOW}],
                        "updated_at": NOW}),
        (c.ContextItem, {"_id": OID, "tower_id": "LGA", "kind": "clearance",
                         "summary": "cleared to land", "callsigns": ["AC8646"],
                         "lane_ids": ["RWY_04_22"], "instruction": "land",
                         "readback": "matched", "source": {"coll": "transmissions", "id": OID},
                         "t": NOW, "expires_at": NOW}),
        (c.Permission, {"_id": "safety", "agent": "safety", "enabled": True,
                        "mode": "proactive", "tier": "needs_approval", "read": ["context"],
                        "write": ["intents_safety"], "by": "setup", "changed_at": NOW}),
        (c.RuleSet, {"version": "normal_v1", "name": "n", "active": True,
                     "rules": [{"id": "runway_incursion", "description": "d"}]}),
        (c.AgentState, {"_id": "safety_agent", "resume_token": {"_data": "x"},
                        "updated_at": NOW}),
        (c.PositionSample, {"t": NOW, "meta": {"object_id": "AC8646", "tower_id": "LGA",
                                               "kind": "aircraft", "callsign": "AC8646"},
                            "position": PT, "status": "airborne", "alt_ft": 300}),
        (c.StateEvent, {"t": NOW, "tower_id": "LGA", "kind": "lane_status",
                        "ref": "RWY_04_22", "doc": {"state": "clear"}, "by": None}),
        (c.AudioClip, {"tower_id": "LGA", "stream_id": "LGA_TWR", "direction": "in",
                       "t_start": NOW, "t_end": NOW, "duration_s": 2.5, "status": "queued",
                       "created_at": NOW}),
        (c.Transmission, {"tower_id": "LGA", "stream_id": "LGA_TWR",
                          "controller_id": "ctl_local", "t": NOW, "direction": "in",
                          "from": "TOWER", "to": "AC8646", "kind": "clearance",
                          "instruction": "land", "runway": "04", "lane_id": "RWY_04_22",
                          "transcript": "AC8646 runway 04 cleared to land",
                          "confidence": 0.95, "readback": "not_applicable",
                          "verified": True, "clip_id": OID}),
        (c.Intent, {"t": NOW, "tower_id": "LGA", "agent": "safety", "action": "stop",
                    "severity": "critical", "target": ["TRUCK1"], "lane_id": "RWY_04_22",
                    "reason": "runway_incursion"}),
        (c.Decision, {"t": NOW, "tower_id": "LGA", "intent_id": OID, "agent": "safety",
                      "intent_action": "stop", "severity": "critical",
                      "outcome": "pending_approval", "rule": "needs_approval",
                      "controller_id": "ctl_local", "mode": "proactive",
                      "authorized_by": {"permission_id": "safety",
                                        "rule_set_version": "normal_v1"}}),
        (c.SecurityEvent, {"t": NOW, "agent": "procedures", "kind": "write_denied",
                           "attempted": "decisions.insert_one"}),
        (c.Weather, {"t": NOW, "tower_id": "LGA", "visibility_sm": 0.75, "wind": "03012KT"}),
        (c.SimRun, {"tower_id": "LGA", "scenario": "incursion", "kind": "incident",
                    "started_at": NOW}),
    ]


@pytest.mark.parametrize(("model", "data"), samples(), ids=lambda x: getattr(x, "__name__", ""))
def test_round_trip(model, data):
    doc = c.to_mongo(model.model_validate(data))
    again = c.to_mongo(model.model_validate(doc))
    assert doc == again
    if "_id" in data:
        assert doc["_id"] == data["_id"]


def test_transmission_uses_from_alias():
    _, data = next(s for s in samples() if s[0] is c.Transmission)
    doc = c.to_mongo(c.Transmission.model_validate(data))
    assert doc["from"] == "TOWER" and "from_" not in doc and "_id" not in doc


def test_enums():
    assert "clear" not in get_args(c.IntentAction)
    assert set(get_args(c.IntentAction)) == {"warn", "stop", "propose_instruction",
                                             "close_lane", "reopen_lane", "set_rule_set"}
    assert get_args(c.PermissionAgent) == (*get_args(c.AgentName), "system")
    assert "report" in get_args(c.TransmissionKind)


def test_model_rules():
    with pytest.raises(ValidationError):
        c.Intent(t=NOW, tower_id="LGA", agent="safety", action="clear", severity="info",
                 reason="x")
    with pytest.raises(ValidationError):
        c.Decision(t=NOW, tower_id="LGA", intent_id=OID, agent="procedures",
                   intent_action="propose_instruction", severity="info", outcome="executed",
                   rule="x", controller_id="ctl_local", mode="proactive",
                   authorized_by={"permission_id": "procedures"})
