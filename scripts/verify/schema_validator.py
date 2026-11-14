"""Check 5: create a collection with a $jsonSchema validator; valid insert passes, invalid is rejected."""
import os
import sys

from pymongo import MongoClient
from pymongo.errors import WriteError

from _env import URI_HELP, require

uri = require("MONGODB_URI", URI_HELP)
db = MongoClient(uri, serverSelectionTimeoutMS=15000)[os.environ.get("MONGODB_DB", "holdshort")]
name = "verify_schema"
try:
    db.drop_collection(name)
    db.create_collection(name, validator={"$jsonSchema": {
        "bsonType": "object",
        "required": ["callsign", "runway"],
        "properties": {
            "callsign": {"bsonType": "string"},
            "runway": {"bsonType": "string", "pattern": "^[0-3][0-9][LRC]?$"},
        },
    }}, validationAction="error", validationLevel="strict")
    db[name].insert_one({"callsign": "TEST123", "runway": "27L"})
    try:
        db[name].insert_one({"callsign": 42})
    except WriteError as e:
        print(f"PASS 5: invalid insert rejected (code {e.code}: {e.details.get('errmsg') if e.details else e})")
    else:
        print("FAIL 5: invalid document was accepted")
        sys.exit(1)
except Exception as e:
    print(f"FAIL 5: {type(e).__name__}: {e}")
    sys.exit(1)
finally:
    try:
        db.drop_collection(name)
    except Exception:
        pass
