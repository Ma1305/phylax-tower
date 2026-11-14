"""Test helper for scripts/pair.mjs: change the scratch database so /viz/live has something to send.

    uv run --project ../api python scripts/poke.py lane|call|drop   (MONGODB_DB must be a scratch db)
"""

import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "api"))

from bson import ObjectId  # noqa: E402
from pymongo import MongoClient  # noqa: E402

import config  # noqa: E402

name = config.db_name()
if name in config.PROTECTED_DBS or "_t" not in name:
    raise SystemExit(f"refusing to touch {name}")
client = MongoClient(config.mongo_uri(), tz_aware=True)
db = client[name]
now = datetime.now(UTC)
what = sys.argv[1]
if what == "lane":  # a lane closes and an object moves
    db.lane_status.update_one(
        {"_id": "TWY_G"},
        {"$set": {"state": "closed", "reason": "Closed for inspection", "updated_at": now}},
    )
    db.objects.update_one({"_id": "OBJ-17"}, {"$set": {"label": "Person on foot (moved)", "updated_at": now}})
elif what == "call":  # a new call with audio on the Ops stream
    src = db.audio_clips.find_one({"stream_id": "LGA_OPS", "direction": "in"})
    clip_id, tx_id = ObjectId(), ObjectId()
    db.audio_clips.insert_one(
        {
            **src,
            "_id": clip_id,
            "t_start": now,
            "t_end": now + timedelta(seconds=2),
            "transmission_id": tx_id,
            "created_at": now,
        }
    )
    tx = db.transmissions.find_one({"clip_id": src["_id"]})
    db.transmissions.insert_one(
        {
            **tx,
            "_id": tx_id,
            "t": now,
            "t_end": now + timedelta(seconds=2),
            "clip_id": clip_id,
            "transcript": "Ops, TRUCK1, holding position at Delta.",
            "kind": "report",
        }
    )
elif what == "drop":
    client.drop_database(name)
print(what, "ok")
client.close()
