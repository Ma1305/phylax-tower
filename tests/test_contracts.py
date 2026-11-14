"""Pydantic contracts round-trip and enforce the plan's rules (no database needed)."""

from datetime import UTC, datetime

import pytest
from bson import ObjectId
from pydantic import ValidationError

from holdshort.contracts import Decision, Intent, Transmission, to_mongo

T = datetime(2026, 3, 22, 23, 36, 40, tzinfo=UTC)


def test_tower_clears_truck1_to_cross_04_at_d_round_trip():
    tx = Transmission.model_validate({
        "t": T, "from": "TOWER", "to": "TRUCK1", "kind": "clearance",
        "instruction": "cross", "runway": "04", "at": "D",
        "transcript": "Truck 1, cross runway 04 at Delta.",
        "confidence": 0.93, "readback": "matched", "verified": True,
        "audio_ref": "audio/tower_truck1_cross.mp3",
    })
    doc = to_mongo(tx)
    assert doc["from"] == "TOWER" and "from_" not in doc
    assert doc["t"] == T
    assert Transmission.model_validate(doc) == tx
    assert Transmission.model_validate_json(tx.model_dump_json(by_alias=True)) == tx


def test_low_confidence_cannot_be_verified():
    with pytest.raises(ValidationError):
        Transmission(t=T, from_="TOWER", to="TRUCK1", kind="clearance", instruction="cross",
                     transcript="...", confidence=0.5, readback="missing", verified=True)


def test_intent_never_clear():
    with pytest.raises(ValidationError):
        Intent(t=T, agent="safety", action="clear", target=["TRUCK1"], reason="x")


def test_naive_datetime_rejected():
    with pytest.raises(ValidationError):
        Intent(t=datetime(2026, 3, 22, 23, 36), agent="safety", action="stop", reason="x")


def test_decision_executed_proposal_needs_approval():
    base = dict(t=T, intent_id=str(ObjectId()), agent="procedures",
                intent_action="propose_instruction", outcome="executed", rule="r",
                authorized_by={"permission_id": ObjectId(), "rule_set_version": "normal_v1"})
    with pytest.raises(ValidationError):
        Decision(**base)
    d = Decision(**base, approved_by="controller_A")
    assert isinstance(d.intent_id, ObjectId)
    assert Decision.model_validate_json(d.model_dump_json()) == d
