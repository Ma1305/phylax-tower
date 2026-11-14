"""Check 4: open a change stream with one client, insert with a second client, see the event."""
import os
import sys
import threading
import time
import uuid

from pymongo import MongoClient

from _env import URI_HELP, redact, require

uri = require("MONGODB_URI", URI_HELP)
db_name = os.environ.get("MONGODB_DB", "holdshort")
coll_name = "verify_change_stream"
marker = str(uuid.uuid4())

watcher = MongoClient(uri, serverSelectionTimeoutMS=15000)
writer = MongoClient(uri, serverSelectionTimeoutMS=15000)
try:
    watcher.admin.command("ping")
    coll = watcher[db_name][coll_name]
    with coll.watch([{"$match": {"operationType": "insert"}}], max_await_time_ms=1000) as stream:
        def insert():
            time.sleep(1)
            writer[db_name][coll_name].insert_one({"marker": marker})
        threading.Thread(target=insert, daemon=True).start()
        deadline = time.time() + 20
        while time.time() < deadline:
            change = stream.try_next()
            if change and change["fullDocument"].get("marker") == marker:
                print(f"PASS 4: change event received for insert into {db_name}.{coll_name}")
                break
        else:
            print("FAIL 4: no change event within 20s")
            sys.exit(1)
except Exception as e:
    print(f"FAIL 4: {type(e).__name__}: {redact(e)}")
    sys.exit(1)
finally:
    try:
        writer[db_name][coll_name].drop()
    except Exception:
        pass
    watcher.close()
    writer.close()
