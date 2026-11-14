"""Atlas $jsonSchema validators enforce the plan's policy layer."""

from datetime import UTC, datetime

import pytest
import setup_atlas
from bson import ObjectId
from pymongo.errors import WriteError

from holdshort.contracts import Decision, Intent, to_mongo

T = datetime(2026, 3, 22, 23, 37, 5, tzinfo=UTC)


def _stop_intent(agent: str = "safety") -> dict:
    return to_mongo(Intent(t=T, agent=agent, action="stop", target=["TRUCK1"], runway="04",
                           reason="AC8646 cleared to land 04", conflicts_with="AC8646"))


def _decision(**kw) -> dict:
    base = dict(t=T, intent_id=ObjectId(), agent="procedures",
                intent_action="propose_instruction", outcome="executed",
                rule="no_crossing_during_active_landing",
                authorized_by={"permission_id": ObjectId(), "rule_set_version": "normal_v1"},
                notified=["TOWER"])
    base.update(kw)
    return base


def test_valid_stop_intent_inserts(test_db):
    assert test_db.intents_safety.insert_one(_stop_intent()).inserted_id


def test_clear_action_rejected(test_db):
    doc = _stop_intent() | {"action": "clear"}
    with pytest.raises(WriteError):
        test_db.intents_safety.insert_one(doc)


def test_identity_is_the_collection(test_db):
    with pytest.raises(WriteError):
        test_db.intents_procedures.insert_one(_stop_intent("safety"))


def test_executed_proposal_without_approval_rejected(test_db):
    with pytest.raises(WriteError):
        test_db.decisions.insert_one(_decision())
    with pytest.raises(WriteError):
        test_db.decisions.insert_one(_decision(approved_by=None))
    with pytest.raises(WriteError):
        test_db.decisions.insert_one(_decision(approved_by=""))


def test_executed_proposal_with_approval_accepted(test_db):
    doc = to_mongo(Decision.model_validate(_decision(approved_by="controller_A")))
    assert test_db.decisions.insert_one(doc).inserted_id


def test_pending_proposal_without_approval_accepted(test_db):
    assert test_db.decisions.insert_one(_decision(outcome="pending_approval")).inserted_id


def test_low_confidence_transmission_cannot_be_verified(test_db):
    doc = {"t": T, "from": "TOWER", "to": "TRUCK1", "kind": "clearance",
           "instruction": "cross", "runway": "04", "at": "D",
           "transcript": "Truck 1 cross runway 04 at D", "confidence": 0.5,
           "readback": "missing", "verified": True, "audio_ref": None}
    with pytest.raises(WriteError):
        test_db.transmissions.insert_one(doc)
    assert test_db.transmissions.insert_one(doc | {"verified": False}).inserted_id


def test_setup_runs_twice(test_db):
    setup_atlas.setup(test_db, log=lambda *_: None)
    setup_atlas.setup(test_db, log=lambda *_: None)
    assert test_db.permissions.count_documents({}) == 5
    assert test_db.rules.count_documents({}) == 2
    assert test_db.rules.find_one({"active": True})["version"] == "normal_v1"
    low_vis = test_db.rules.find_one({"version": "low_vis_v1"})
    assert "low_vis_one_movement" in [r["id"] for r in low_vis["rules"]]
    # validators survive collMod: "clear" is still rejected
    with pytest.raises(WriteError):
        test_db.intents_weather.insert_one(_stop_intent("weather") | {"action": "clear"})
