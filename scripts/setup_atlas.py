"""Create (or update) the Phylax Tower v2 collections, validators, indexes and seed data.

Idempotent: run it as often as you like. Existing collections get their validator replaced
with collMod; indexes are created if missing; seed documents are upserted. Seeding never
overwrites live switches (permission enabled / mode / tier, the active rule set flag,
lane_status) - those change at runtime.

    uv run python scripts/setup_atlas.py               # database holdshort_v2 (or MONGODB_DB)
    uv run python scripts/setup_atlas.py --db NAME     # another database
    uv run python scripts/setup_atlas.py --reset-live  # also clear objects/context, lanes clear
"""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime
from typing import Any, get_args

from pymongo import ASCENDING, DESCENDING, GEOSPHERE
from pymongo.database import Database
from pymongo.errors import PyMongoError

from holdshort import contracts as c
from holdshort.db import get_client
from holdshort.env import mongodb_db_name, redact
from holdshort.world import clear_cache, lane_area, load_world_file

INTENT_COLLECTIONS = {f"intents_{a}": a for a in c.AGENTS_WITH_INTENTS}

COLLECTIONS = [
    # world
    "towers", "lanes", "hold_points", "controllers", "streams",
    # live
    "objects", "lane_status", "context", "permissions", "rules", "agent_state",
    # logs
    "positions", "state_events", "audio_clips", "transmissions", *INTENT_COLLECTIONS,
    "decisions", "security_events", "weather", "sim_runs",
    # kept from v1 (Atlas Search / Vector Search, see scripts/setup_search.py)
    "callsigns", "incidents",
]

POSITIONS_TS = {"timeField": "t", "metaField": "meta", "granularity": "seconds"}
POSITIONS_TTL_S = 86400

# ------------------------------------------------------------------ schema helpers


def _enum(literal: Any) -> dict:
    return {"bsonType": "string", "enum": list(get_args(literal))}


def _nullable(schema: dict) -> dict:
    out = dict(schema)
    bt = schema["bsonType"]
    out["bsonType"] = [*(bt if isinstance(bt, list) else [bt]), "null"]
    if "enum" in out:
        out["enum"] = [*out["enum"], None]
    return out


STR = {"bsonType": "string"}
DATE = {"bsonType": "date"}
BOOL = {"bsonType": "bool"}
NUM = {"bsonType": ["double", "int", "long", "decimal"]}
OID = {"bsonType": "objectId"}
ID = {"bsonType": ["objectId", "string"]}
CALLSIGN = {"bsonType": "string", "pattern": c.CALLSIGN_PATTERN}
RUNWAY = {"bsonType": "string", "pattern": c.RUNWAY_PATTERN}
STR_LIST = {"bsonType": "array", "items": STR}
N_STR = _nullable(STR)
N_OID = _nullable(OID)


def _obj(required: list[str], properties: dict, **extra) -> dict:
    return {"bsonType": "object", "required": required, "properties": properties, **extra}


def transmissions_schema() -> dict:
    return _obj(
        ["tower_id", "stream_id", "t", "direction", "from", "to", "kind", "instruction",
         "transcript", "confidence", "readback", "verified"],
        {
            "tower_id": STR, "stream_id": STR, "controller_id": N_STR,
            "t": DATE, "t_end": _nullable(DATE), "direction": _enum(c.Direction),
            "from": CALLSIGN, "to": CALLSIGN,
            "kind": _enum(c.TransmissionKind), "instruction": _enum(c.Instruction),
            "runway": _nullable(RUNWAY), "at": N_STR, "lane_id": N_STR,
            "transcript": STR,
            "confidence": {**NUM, "minimum": 0, "maximum": 1},
            "readback": _enum(c.Readback), "verified": BOOL, "flag": N_STR,
            "clip_id": N_OID, "audio_id": N_OID, "synthetic": BOOL, "decision_id": N_OID,
        },
        # low-confidence transmissions can never be verified
        anyOf=[
            {"properties": {"verified": {"enum": [False]}}},
            {"properties": {"confidence": {"minimum": c.CONFIDENCE_VERIFIED_MIN}}},
        ],
    )


def intents_schema(agent: str) -> dict:
    """intents_<agent>: identity = collection; action enum has no "clear"."""
    return _obj(
        ["t", "tower_id", "agent", "action", "severity", "reason"],
        {
            "t": DATE, "tower_id": STR,
            "agent": {"bsonType": "string", "enum": [agent]},
            "action": _enum(c.IntentAction), "severity": _enum(c.Severity),
            "target": {"bsonType": "array", "items": CALLSIGN},
            "object_ids": STR_LIST,
            "lane_id": N_STR, "runway": _nullable(RUNWAY), "stream_id": N_STR,
            "reason": STR, "conflicts_with": N_STR, "proposal_text": N_STR, "detail": N_STR,
        },
    )


def decisions_schema() -> dict:
    """decisions: an executed propose_instruction requires a non-empty approved_by."""
    return _obj(
        ["t", "tower_id", "intent_id", "agent", "intent_action", "severity", "outcome",
         "rule", "controller_id", "mode", "authorized_by"],
        {
            "t": DATE, "tower_id": STR, "intent_id": ID,
            "agent": _enum(c.AgentName), "intent_action": _enum(c.IntentAction),
            "severity": _enum(c.Severity), "outcome": _enum(c.DecisionOutcome),
            "rule": STR, "lane_id": N_STR, "controller_id": STR, "stream_id": N_STR,
            "text": N_STR, "target": STR_LIST, "mode": _enum(c.AgentMode),
            "authorized_by": _obj(["permission_id"], {"permission_id": ID,
                                                      "rule_set_version": N_STR}),
            "approved_by": N_STR, "revokes": _nullable(ID), "notified": STR_LIST,
        },
        anyOf=[
            {"properties": {"intent_action": {"enum": [
                a for a in get_args(c.IntentAction) if a != "propose_instruction"]}}},
            {"properties": {"outcome": {"enum": [
                o for o in get_args(c.DecisionOutcome) if o != "executed"]}}},
            {"required": ["approved_by"],
             "properties": {"approved_by": {"bsonType": "string", "minLength": 1}}},
        ],
    )


def audio_clips_schema() -> dict:
    return _obj(
        ["tower_id", "stream_id", "direction", "t_start", "t_end", "duration_s", "status",
         "created_at"],
        {
            "tower_id": STR, "stream_id": STR, "direction": _enum(c.Direction),
            "t_start": DATE, "t_end": DATE, "duration_s": {**NUM, "minimum": 0},
            "audio_id": N_OID, "mime": N_STR, "truth_text": N_STR, "speaker": N_STR,
            "status": _enum(c.ClipStatus), "transmission_id": N_OID, "error": N_STR,
            "created_at": DATE,
        },
    )


def security_events_schema() -> dict:
    return _obj(
        ["t", "kind", "attempted"],
        {
            "t": DATE, "agent": N_STR, "controller_id": N_STR,
            "kind": _enum(c.SecurityEventKind), "attempted": STR,
            "error_code": {"bsonType": ["int", "long", "null"]}, "error": N_STR,
        },
        anyOf=[{"required": ["agent"]}, {"required": ["controller_id"]}],
    )


def state_events_schema() -> dict:
    return _obj(
        ["t", "tower_id", "kind", "ref"],
        {"t": DATE, "tower_id": STR, "kind": _enum(c.StateEventKind), "ref": STR,
         "doc": {"bsonType": ["object", "null"]}, "by": N_STR},
    )


def weather_schema() -> dict:
    return _obj(
        ["t", "tower_id", "visibility_sm"],
        {"t": DATE, "tower_id": STR, "visibility_sm": {**NUM, "minimum": 0},
         "wind": N_STR, "raw": N_STR},
    )


VALIDATORS: dict[str, dict] = {
    "transmissions": transmissions_schema(),
    **{name: intents_schema(agent) for name, agent in INTENT_COLLECTIONS.items()},
    "decisions": decisions_schema(),
    "audio_clips": audio_clips_schema(),
    "security_events": security_events_schema(),
    "state_events": state_events_schema(),
    "weather": weather_schema(),
}

# ------------------------------------------------------------------------- seeds

_READ = {
    "listener": ["audio_clips", "streams", "controllers", "lanes", "objects", "callsigns",
                 "audio.files", "audio.chunks"],
    "safety": ["context", "objects", "lane_status", "lanes", "controllers", "streams", "rules",
               "permissions", "weather"],
    "procedures": ["context", "objects", "lane_status", "lanes", "controllers", "streams",
                   "rules", "decisions", "intents_safety", "incidents", "permissions"],
    "weather": ["weather", "rules"],
    "system": [],
}
_WRITE = {
    "listener": ["transmissions", "audio_clips"],
    "safety": ["intents_safety"],
    "procedures": ["intents_procedures"],
    "weather": ["intents_weather"],
    "system": [],
}
PERMISSION_SEED: dict[str, dict[str, Any]] = {
    "safety": {"mode": "proactive", "tier": "autonomous"},
    "procedures": {"mode": "proactive", "tier": "needs_approval"},
    "weather": {"mode": "proactive", "tier": "autonomous"},
    "listener": {"mode": "reactive", "tier": "autonomous"},
    "system": {"mode": "proactive", "tier": "autonomous"},
}

_BASE_RULES = [
    {"id": "no_crossing_during_active_landing",
     "description": "No runway crossing while an aircraft is cleared to land on that runway."},
    {"id": "no_landing_on_occupied_runway",
     "description": "No landing while a vehicle or aircraft is on or cleared onto the runway."},
    {"id": "runway_incursion",
     "description": "An object is on a runway another object is cleared to land on or take "
                    "off from, and has no clearance for it."},
    {"id": "uncleared_entry",
     "description": "An object is on a runway with no clearance at all."},
    {"id": "readback_mismatch",
     "description": "A readback does not match the instruction it answers."},
    {"id": "no_ai_clearances",
     "description": "Agents may warn, stop traffic or propose instructions, never clear it.",
     "params": {"forbidden_actions": ["clear"]}},
]

RULE_SETS = [
    {"version": "normal_v1", "name": "Normal operations", "active": True, "rules": _BASE_RULES},
    {"version": "low_vis_v1", "name": "Low visibility operations", "active": False,
     "rules": [*_BASE_RULES,
               {"id": "low_vis_one_movement",
                "description": "Low visibility: only one movement on a runway at a time.",
                "params": {"max_movements_per_runway": 1}}]},
]

# ------------------------------------------------------------------------- setup


def _ensure_collection(db: Database, name: str, existing: set[str]) -> str:
    if name == "positions":
        if name not in existing:
            db.create_collection(name, timeseries=POSITIONS_TS,
                                 expireAfterSeconds=POSITIONS_TTL_S)
            return "created (time series)"
        db.command("collMod", name, expireAfterSeconds=POSITIONS_TTL_S)
        return "exists (time series)"
    validator = VALIDATORS.get(name)
    opts: dict[str, Any] = {}
    if validator:
        opts = {"validator": {"$jsonSchema": validator},
                "validationLevel": "strict", "validationAction": "error"}
    if name not in existing:
        db.create_collection(name, **opts)
        return "created" + (" + validator" if opts else "")
    if opts:
        db.command("collMod", name, **opts)
        return "exists, validator updated"
    return "exists"


def _indexes(db: Database) -> int:
    specs: list[tuple[str, list, dict]] = [
        ("lanes", [("area", GEOSPHERE)], {}),
        ("lanes", [("tower_id", ASCENDING)], {}),
        ("objects", [("expires_at", ASCENDING)], {"expireAfterSeconds": 0}),
        ("objects", [("tower_id", ASCENDING), ("lane_id", ASCENDING)], {}),
        ("context", [("expires_at", ASCENDING)], {"expireAfterSeconds": 0}),
        ("context", [("tower_id", ASCENDING), ("kind", ASCENDING), ("t", DESCENDING)], {}),
        ("context", [("callsigns", ASCENDING)], {}),
        ("context", [("lane_ids", ASCENDING)], {}),
        ("state_events", [("tower_id", ASCENDING), ("kind", ASCENDING), ("ref", ASCENDING),
                          ("t", ASCENDING)], {}),
        ("transmissions", [("t", DESCENDING)], {}),
        ("transmissions", [("stream_id", ASCENDING), ("t", ASCENDING)], {}),
        ("decisions", [("t", DESCENDING)], {}),
        ("decisions", [("intent_id", ASCENDING)], {}),
        ("decisions", [("controller_id", ASCENDING), ("outcome", ASCENDING)], {}),
        ("weather", [("t", DESCENDING)], {}),
        ("security_events", [("t", DESCENDING)], {}),
        ("audio_clips", [("status", ASCENDING), ("created_at", ASCENDING)], {}),
        ("audio_clips", [("stream_id", ASCENDING), ("t_start", ASCENDING)], {}),
        ("rules", [("version", ASCENDING)], {"unique": True}),
        *[(name, [("t", DESCENDING)], {}) for name in INTENT_COLLECTIONS],
    ]
    for coll, keys, opts in specs:
        db[coll].create_index(keys, **opts)
    return len(specs)


def _state_event(db: Database, kind: str, ref: str, doc: dict, by: str, now: datetime,
                 tower_id: str = c.TOWER_ID) -> None:
    db.state_events.insert_one({"t": now, "tower_id": tower_id, "kind": kind, "ref": ref,
                                "doc": doc, "by": by})


def _seed_world(db: Database, tower_id: str, log) -> dict:
    w = load_world_file(tower_id)
    if not w:
        raise SystemExit(f"no world file for tower {tower_id}")
    tower = c.to_mongo(c.Tower.model_validate(w["tower"]))
    db.towers.replace_one({"_id": tower["_id"]}, tower, upsert=True)
    counts = {}
    for coll, model in (("lanes", c.Lane), ("hold_points", c.HoldPoint),
                        ("controllers", c.Controller), ("streams", c.Stream)):
        n = 0
        for raw in w[coll]:
            d = {k: v for k, v in raw.items() if not k.startswith("_comment")}
            d["tower_id"] = tower_id
            if coll == "lanes" and not d.get("area"):
                d["area"] = lane_area(d["centerline"], d["width_m"])
            doc = c.to_mongo(model.model_validate(d))
            db[coll].replace_one({"_id": doc["_id"]}, doc, upsert=True)
            n += 1
        counts[coll] = n
    log(f"  world {tower_id}: 1 tower, " + ", ".join(f"{n} {k}" for k, n in counts.items()))
    return w


def _clear_lane_status(lane: dict, owner: str | None, now: datetime) -> dict:
    return c.to_mongo(c.LaneStatus(
        _id=lane["_id"], tower_id=lane["tower_id"], controller_id=owner or "",
        state="clear", occupants=[], reservations=[], reason=None, updated_at=now))


def _owner(controllers: list[dict], lane_id: str) -> str | None:
    for ctl in controllers:
        if lane_id in ctl.get("lanes", []) and ctl.get("role") != "supervisor":
            return ctl["_id"]
    return next((ctl["_id"] for ctl in controllers if ctl.get("role") == "supervisor"), None)


SEED_BY = "setup_script"
# `by` values that mean "only the setup script ever wrote this switch" ("setup" = older seed)
_SEED_BYS = (SEED_BY, "setup")


def _migrate_autonomous_stops(db: Database, now: datetime) -> list[str]:
    """Stops are autonomous by default (product owner, 13:55). Move a seeded permission that
    no controller has changed from needs_approval to its seed tier, with a state event."""
    out = []
    for agent, sw in PERMISSION_SEED.items():
        if sw["tier"] != "autonomous":
            continue
        doc = db.permissions.find_one_and_update(
            {"_id": agent, "tier": "needs_approval", "by": {"$in": list(_SEED_BYS)}},
            {"$set": {"tier": "autonomous", "by": SEED_BY, "changed_at": now}},
            return_document=True)
        if doc is not None:
            _state_event(db, "permission", agent, doc, SEED_BY, now)
            out.append(agent)
    return out


def setup(db: Database, log=print, reset_live: bool = False,
          tower_id: str = c.TOWER_ID) -> None:
    """Create/update every collection, validator, index and seed document in `db`."""
    existing = set(db.list_collection_names())
    for name in COLLECTIONS:
        log(f"  {name}: {_ensure_collection(db, name, existing)}")
    log(f"  indexes: {_indexes(db)} ensured")

    now = datetime.now(UTC)
    _seed_world(db, tower_id, log)
    clear_cache()

    new = 0
    for agent, sw in PERMISSION_SEED.items():
        res = db.permissions.update_one(
            {"_id": agent},
            {"$set": {"agent": agent, "read": _READ[agent], "write": _WRITE[agent]},
             "$setOnInsert": {"enabled": True, "mode": sw["mode"], "tier": sw["tier"],
                              "by": SEED_BY, "changed_at": now}},
            upsert=True)
        if res.upserted_id is not None:
            new += 1
            _state_event(db, "permission", agent, db.permissions.find_one({"_id": agent}),
                         SEED_BY, now)
    log(f"  permissions: {len(PERMISSION_SEED)} ({new} new; live switches kept)")
    migrated = _migrate_autonomous_stops(db, now)
    if migrated:
        log(f"  permissions: {', '.join(migrated)} needs_approval -> autonomous "
            "(seed default changed; no controller had changed it)")

    for rs in RULE_SETS:
        doc = c.to_mongo(c.RuleSet.model_validate(rs))
        active = doc.pop("active")
        db.rules.update_one({"version": doc["version"]},
                            {"$set": doc, "$setOnInsert": {"active": active}}, upsert=True)
    log(f"  rules: {len(RULE_SETS)} rule sets upserted (active flag kept)")

    controllers = list(db.controllers.find({"tower_id": tower_id}))
    lanes = list(db.lanes.find({"tower_id": tower_id}))
    new = 0
    for lane in lanes:
        doc = _clear_lane_status(lane, _owner(controllers, lane["_id"]), now)
        if reset_live:
            db.lane_status.replace_one({"_id": lane["_id"]}, doc, upsert=True)
            changed = True
        else:
            changed = db.lane_status.update_one(
                {"_id": lane["_id"]}, {"$setOnInsert": doc}, upsert=True
            ).upserted_id is not None
        if changed:
            new += 1
            _state_event(db, "lane_status", lane["_id"], doc,
                         "setup_reset" if reset_live else "setup", now)
    log(f"  lane_status: {len(lanes)} lanes ({new} set to clear)")

    if reset_live:
        n_obj = db.objects.delete_many({}).deleted_count
        n_ctx = db.context.delete_many({}).deleted_count
        log(f"  reset-live: removed {n_obj} objects, {n_ctx} context items")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--db", default=None, help="database name (default: MONGODB_DB)")
    parser.add_argument("--reset-live", action="store_true",
                        help="clear objects and context, set every lane_status to clear")
    args = parser.parse_args(argv)
    name = args.db or mongodb_db_name()
    if name == "holdshort":
        print("FAIL: refusing to touch the v1 database 'holdshort'")
        return 1
    client = get_client()
    try:
        print(f"Setting up database {name!r}")
        setup(client[name], reset_live=args.reset_live)
        print("OK")
        return 0
    except PyMongoError as e:
        print(f"FAIL: {redact(e)}")
        return 1
    finally:
        client.close()


if __name__ == "__main__":
    sys.exit(main())
