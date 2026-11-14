"""Create (or update) the Hold Short collections, validators, indexes and seed data.

Idempotent: run it as often as you like. Existing collections get their
validator replaced with collMod; indexes are created if missing; seed documents
are upserted. Seeding never overwrites live permission switches or the active
rule set flag (those are changed by controllers at runtime).

    uv run python scripts/setup_atlas.py            # database from MONGODB_DB (default holdshort)
    uv run python scripts/setup_atlas.py --db NAME  # another database (tests use <db>_test)
"""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime
from typing import Any, get_args

from pymongo import ASCENDING, DESCENDING
from pymongo.database import Database
from pymongo.errors import PyMongoError

from holdshort import contracts as c
from holdshort.db import get_client
from holdshort.env import mongodb_db_name, redact

INTENT_COLLECTIONS = {f"intents_{a}": a for a in c.AGENTS_WITH_INTENTS}

COLLECTIONS = [
    "transmissions",
    "flight_state",
    "runway_status",
    "rules",
    "permissions",
    "decisions",
    *INTENT_COLLECTIONS,
    "gate_state",
    "metar",
    "security_events",
]

# ------------------------------------------------------------------ schema helpers


def _enum(literal: Any) -> dict:
    return {"bsonType": "string", "enum": list(get_args(literal))}


def _nullable(schema: dict) -> dict:
    out = dict(schema)
    out["bsonType"] = [schema["bsonType"], "null"]
    if "enum" in out:
        out["enum"] = [*out["enum"], None]
    return out


STR = {"bsonType": "string"}
DATE = {"bsonType": "date"}
BOOL = {"bsonType": "bool"}
ID = {"bsonType": ["objectId", "string"]}
CALLSIGN = {"bsonType": "string", "pattern": c.CALLSIGN_PATTERN}
RUNWAY = {"bsonType": "string", "pattern": c.RUNWAY_PATTERN}
STR_LIST = {"bsonType": "array", "items": STR}


def transmissions_schema() -> dict:
    """$jsonSchema for transmissions (mirrors contracts.Transmission)."""
    return {
        "bsonType": "object",
        "required": [
            "t", "from", "to", "kind", "instruction", "transcript",
            "confidence", "readback", "verified",
        ],
        "properties": {
            "t": DATE,
            "from": CALLSIGN,
            "to": CALLSIGN,
            "kind": _enum(c.TransmissionKind),
            "instruction": _enum(c.Instruction),
            "runway": _nullable(RUNWAY),
            "at": _nullable(STR),
            "transcript": STR,
            "confidence": {"bsonType": ["double", "int"], "minimum": 0, "maximum": 1},
            "readback": _enum(c.Readback),
            "verified": BOOL,
            "audio_ref": _nullable(STR),
        },
        # low-confidence transmissions can never be verified
        "anyOf": [
            {"properties": {"verified": {"enum": [False]}}},
            {"properties": {"confidence": {"minimum": c.CONFIDENCE_VERIFIED_MIN}}},
        ],
    }


def intents_schema(agent: str) -> dict:
    """$jsonSchema for intents_<agent>: identity = collection; never "clear"."""
    return {
        "bsonType": "object",
        "required": ["t", "agent", "action"],
        "properties": {
            "t": DATE,
            "agent": {"bsonType": "string", "enum": [agent]},
            "action": _enum(c.IntentAction),
            "target": {"bsonType": "array", "items": CALLSIGN},
            "runway": _nullable(RUNWAY),
            "reason": STR,
            "conflicts_with": _nullable(STR),
            "proposal_text": _nullable(STR),
        },
    }


def decisions_schema() -> dict:
    """$jsonSchema for decisions: executed propose_instruction requires approved_by."""
    return {
        "bsonType": "object",
        "required": [
            "t", "intent_id", "agent", "intent_action", "outcome", "rule", "authorized_by",
        ],
        "properties": {
            "t": DATE,
            "intent_id": ID,
            "agent": _enum(c.AgentName),
            "intent_action": _enum(c.IntentAction),
            "outcome": _enum(c.DecisionOutcome),
            "rule": STR,
            "authorized_by": {
                "bsonType": "object",
                "required": ["permission_id", "rule_set_version"],
                "properties": {"permission_id": ID, "rule_set_version": STR},
            },
            "approved_by": _nullable(STR),
            "revokes": {"bsonType": ["objectId", "string", "null"]},
            "notified": STR_LIST,
        },
        "anyOf": [
            # not (propose_instruction AND executed) ...
            {"properties": {"intent_action": {"enum": [
                a for a in get_args(c.IntentAction) if a != "propose_instruction"
            ]}}},
            {"properties": {"outcome": {"enum": [
                o for o in get_args(c.DecisionOutcome) if o != "executed"
            ]}}},
            # ... or a non-empty approved_by
            {"required": ["approved_by"],
             "properties": {"approved_by": {"bsonType": "string", "minLength": 1}}},
        ],
    }


def metar_schema() -> dict:
    """$jsonSchema for metar (mirrors contracts.Metar)."""
    return {
        "bsonType": "object",
        "required": ["t", "visibility_sm", "wind"],
        "properties": {
            "t": DATE,
            "visibility_sm": {"bsonType": ["double", "int"], "minimum": 0},
            "wind": STR,
        },
    }


def security_events_schema() -> dict:
    """$jsonSchema for security_events (mirrors contracts.SecurityEvent; insert-only)."""
    return {
        "bsonType": "object",
        "required": ["t", "agent", "attempted", "error"],
        "properties": {
            "t": DATE,
            "agent": _enum(c.AgentName),
            "attempted": STR,
            "error_code": {"bsonType": ["int", "long", "null"]},
            "error": STR,
        },
    }


VALIDATORS: dict[str, dict] = {
    "transmissions": transmissions_schema(),
    "metar": metar_schema(),
    "security_events": security_events_schema(),
    "decisions": decisions_schema(),
    **{name: intents_schema(agent) for name, agent in INTENT_COLLECTIONS.items()},
}

# ------------------------------------------------------------------------- seeds

PERMISSION_SEED: dict[str, str] = {
    "safety": "autonomous",
    "listener": "autonomous",
    "procedures": "needs_approval",
    "weather": "autonomous",  # demo: weather may switch to the low-visibility rule set itself
    "system": "autonomous",
}

_BASE_RULES = [
    {"id": "no_crossing_during_active_landing",
     "description": "No runway crossing while an aircraft is cleared to land on that runway.",
     "params": {}},
    {"id": "no_landing_on_occupied_runway",
     "description": "No landing while a vehicle or aircraft is on or cleared onto the runway.",
     "params": {}},
    {"id": "no_ai_clearances",
     "description": "Agents may stop traffic or propose instructions, never clear it.",
     "params": {"forbidden_actions": ["clear"]}},
]

RULE_SETS = [
    {"version": "normal_v1", "name": "Normal operations", "active": True,
     "rules": _BASE_RULES},
    {"version": "low_vis_v1", "name": "Low visibility operations", "active": False,
     "rules": [*_BASE_RULES,
               {"id": "low_vis_one_movement",
                "description": "Low visibility: only one movement on a runway at a time.",
                "params": {"max_movements_per_runway": 1}}]},
]

# ------------------------------------------------------------------------- setup


def _ensure_collection(db: Database, name: str, existing: set[str]) -> str:
    validator = VALIDATORS.get(name)
    opts: dict[str, Any] = {}
    if validator:
        opts = {"validator": {"$jsonSchema": validator},
                "validationLevel": "strict", "validationAction": "error"}
    if name not in existing:
        db.create_collection(name, **opts)
        return "created"
    if opts:
        db.command("collMod", name, **opts)
        return "updated validator"
    return "exists"


def setup(db: Database, log=print) -> None:
    """Create/update every collection, validator, index and seed document in `db`."""
    existing = set(db.list_collection_names())
    for name in COLLECTIONS:
        log(f"  {name}: {_ensure_collection(db, name, existing)}")

    db.flight_state.create_index([("expires_at", ASCENDING)], expireAfterSeconds=0,
                                 name="ttl_expires_at")
    db.permissions.create_index([("agent", ASCENDING)], unique=True, name="uniq_agent")
    db.rules.create_index([("version", ASCENDING)], unique=True, name="uniq_version")
    db.transmissions.create_index([("t", DESCENDING)], name="t_desc")
    db.decisions.create_index([("t", DESCENDING)], name="t_desc")
    db.security_events.create_index([("t", DESCENDING)], name="t_desc")
    db.metar.create_index([("t", DESCENDING)], name="t_desc")
    log("  indexes: ok")

    now = datetime.now(UTC)
    for agent, tier in PERMISSION_SEED.items():
        doc = c.Permission(agent=agent, enabled=True, tier=tier, by="setup_script",
                           changed_at=now)
        db.permissions.update_one({"agent": agent}, {"$setOnInsert": c.to_mongo(doc)},
                                  upsert=True)
    # Older databases were seeded with weather=needs_approval. Move it to the demo tier,
    # but only if no controller has touched it since setup.
    db.permissions.update_one(
        {"agent": "weather", "by": "setup_script", "tier": {"$ne": PERMISSION_SEED["weather"]}},
        {"$set": {"tier": PERMISSION_SEED["weather"]}},
    )
    log(f"  permissions: {len(PERMISSION_SEED)} seeded (existing switches kept)")

    for rs in RULE_SETS:
        doc = c.to_mongo(c.RuleSet.model_validate(rs))
        active = doc.pop("active")
        db.rules.update_one({"version": doc["version"]},
                            {"$set": doc, "$setOnInsert": {"active": active}}, upsert=True)
    log(f"  rules: {len(RULE_SETS)} rule sets upserted (active flag kept)")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--db", default=None, help="database name (default: MONGODB_DB)")
    args = parser.parse_args(argv)
    name = args.db or mongodb_db_name()
    client = get_client()
    try:
        print(f"Setting up database {name!r}")
        setup(client[name])
        print("OK")
        return 0
    except PyMongoError as e:
        print(f"FAIL: {redact(e)}")
        return 1
    finally:
        client.close()


if __name__ == "__main__":
    sys.exit(main())
