"""Create the search collections, seed them, build the search indexes and wait until ready.

Idempotent: collections get their validator (re)applied, seed documents are
upserted, missing indexes are created, and changed index definitions are updated.

    uv run python scripts/setup_search.py            # database from MONGODB_DB (default holdshort)
    uv run python scripts/setup_search.py --db NAME
    uv run python scripts/setup_search.py --no-vector   # Atlas Search text only

Collections: callsigns (Atlas Search, fuzzy) and incidents (Vector Search with
automated Voyage embedding on `summary`, plus an Atlas Search text index used as
the fallback when the vector index is missing or not queryable).
"""

from __future__ import annotations

import argparse
import sys
import time

from pymongo.collection import Collection
from pymongo.database import Database
from pymongo.errors import OperationFailure, PyMongoError
from pymongo.operations import SearchIndexModel

from holdshort.db import get_client
from holdshort.env import mongodb_db_name, redact
from holdshort.search import callsigns, incidents
from holdshort.search.seed import CALLSIGNS, INCIDENTS

STR = {"bsonType": "string"}

VALIDATORS = {
    callsigns.COLLECTION: {
        "bsonType": "object",
        "required": ["_id", "callsign", "spoken", "kind", "frequency"],
        "properties": {
            "_id": STR,
            "callsign": {"bsonType": "string", "pattern": r"^[A-Z0-9]+$"},
            "spoken": STR,
            "kind": {"bsonType": "string", "enum": ["aircraft", "vehicle", "station"]},
            "frequency": STR,
        },
    },
    incidents.COLLECTION: {
        "bsonType": "object",
        "required": ["_id", "title", "airport", "date", "summary", "source"],
        "properties": {
            "_id": STR,
            "title": STR,
            "airport": STR,
            "summary": STR,
            "source": STR,
            "date": {"bsonType": "string", "pattern": r"^\d{4}-\d{2}-\d{2}$"},
            "tags": {"bsonType": "array", "items": STR},
        },
    },
}


def ensure_collection(db: Database, name: str) -> Collection:
    validator = {"$jsonSchema": VALIDATORS[name]}
    if name in db.list_collection_names():
        db.command("collMod", name, validator=validator)
    else:
        db.create_collection(name, validator=validator)
    return db[name]


def seed(coll: Collection, docs: list[dict]) -> int:
    changed = 0
    for doc in docs:
        r = coll.replace_one({"_id": doc["_id"]}, doc, upsert=True)
        changed += int(bool(r.upserted_id) or r.modified_count)
    return changed


def ensure_index(coll: Collection, name: str, kind: str, definition: dict) -> str:
    existing = next(iter(coll.list_search_indexes(name)), None)
    if existing is None:
        coll.create_search_index(SearchIndexModel(definition, name=name, type=kind))
        return "created"
    if existing.get("latestDefinition") != definition:
        coll.update_search_index(name, definition)
        return "updated"
    return "unchanged"


def wait_ready(coll: Collection, name: str, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        ix = next(iter(coll.list_search_indexes(name)), None)
        if ix and ix.get("status") == "FAILED":
            print(f"  {coll.name}.{name}: FAILED {ix.get('statusDetail')}")
            return False
        if ix and ix.get("queryable") and ix.get("status") == "READY":
            return True
        time.sleep(5)
    return False


def wait_embedded(db: Database, timeout: float) -> bool:
    """Wait until every incident is returned by the vector index (embeddings caught up)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        hits = incidents.similar("runway", k=len(INCIDENTS), db=db, method="vector")
        if len(hits) >= len(INCIDENTS):
            return True
        time.sleep(5)
    return False


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--db", default=None)
    ap.add_argument("--no-vector", action="store_true", help="skip the autoEmbed vector index")
    ap.add_argument("--timeout", type=float, default=600, help="seconds to wait per index")
    args = ap.parse_args()

    try:
        db = get_client()[args.db or mongodb_db_name()]
        cs = ensure_collection(db, callsigns.COLLECTION)
        inc = ensure_collection(db, incidents.COLLECTION)
        seed_docs = [{**d, "callsign": d["_id"]} for d in CALLSIGNS]
        print(f"callsigns: {seed(cs, seed_docs)} upserted/changed of {len(seed_docs)}")
        print(f"incidents: {seed(inc, INCIDENTS)} upserted/changed of {len(INCIDENTS)}")

        wanted = [
            (cs, callsigns.INDEX, "search", callsigns.INDEX_DEFINITION),
            (inc, incidents.TEXT_INDEX, "search", incidents.TEXT_DEFINITION),
        ]
        if not args.no_vector:
            wanted.append(
                (inc, incidents.VECTOR_INDEX, "vectorSearch", incidents.VECTOR_DEFINITION)
            )

        vector_ok = False
        for coll, name, kind, definition in wanted:
            try:
                print(f"{coll.name}.{name} ({kind}): {ensure_index(coll, name, kind, definition)}")
            except OperationFailure as e:
                if kind != "vectorSearch":
                    raise
                print(
                    f"{coll.name}.{name}: not supported here ({redact(e)}); "
                    "incidents.similar() will use the Atlas Search text index"
                )
                continue
            ok = wait_ready(coll, name, args.timeout)
            print(f"  {coll.name}.{name}: {'READY' if ok else 'NOT READY'}")
            if not ok and kind != "vectorSearch":
                return 1
            if kind == "vectorSearch":
                vector_ok = ok

        if vector_ok:
            done = wait_embedded(db, args.timeout)
            print(f"incidents embeddings: {'complete' if done else 'still syncing'}")
        print(f"incidents.similar() method: {'vector' if vector_ok else 'text'}")
    except PyMongoError as e:
        print(f"ERROR: {redact(e)}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
