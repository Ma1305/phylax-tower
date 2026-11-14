"""Live check of generated identities against the REAL Atlas project (operator use only).

    export ATLAS_PROJECT_ID=... AGENT_IDENTITIES=generated
    uv run python scripts/setup_atlas.py --db holdshort_v2_t<N>_id
    uv run python -m holdshort.identity.live_check --db holdshort_v2_t<N>_id

Refuses any database not named ``holdshort_v2_t<N>_id``, and refuses to start when users or
roles named ``hs-*`` / ``hs_*`` already exist (it only ever touches what it created). Steps:
provision safety (time-to-usable), prove Atlas enforces the lists with a raw MongoClient as
that user (code 13 Unauthorized from the server, no ScopedDb), change the read list and let
``sync`` follow it, revoke, confirm by GET that no hs_* / hs-* remain, drop the database.
Prints names and timings only: never a key, password or connection string.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import threading
import time
from datetime import UTC, datetime

from pymongo import MongoClient
from pymongo.errors import OperationFailure

import holdshort.env  # noqa: F401
from holdshort.contracts import Intent, to_mongo
from holdshort.db import get_client
from holdshort.env import redact
from holdshort.identity.atlas import credentials, enabled
from holdshort.identity.core import IdentityManager, role_name

UNAUTHORIZED = 13


def ours(admin) -> tuple[list[str], list[str]]:
    users = [u["username"] for u in admin.list_users() if u["username"].startswith("hs-")]
    roles = [r["roleName"] for r in admin.list_roles() if r["roleName"].startswith("hs_")]
    return users, roles


def attempt(label: str, fn, expect_ok: bool, results: list) -> None:
    try:
        fn()
        ok, got = expect_ok, "allowed"
    except OperationFailure as e:
        got = f"server error code {e.code} ({e.details.get('codeName') if e.details else ''})"
        ok = (not expect_ok) and e.code == UNAUTHORIZED
    results.append(ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}: {got}")


def intent(agent: str) -> dict:
    return to_mongo(Intent(t=datetime.now(UTC), tower_id="live-check", agent=agent,
                           action="warn", severity="info", reason="identity live check"))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    a = ap.parse_args(argv)
    if not re.fullmatch(r"holdshort_v2_t\d+_id", a.db):
        print("refusing: --db must be holdshort_v2_t<N>_id")
        return 2
    creds = credentials()
    if not (enabled() and creds):
        print("BLOCKED: needs AGENT_IDENTITIES=generated and ATLAS_* credentials")
        return 2
    m = IdentityManager(creds)
    pre_users, pre_roles = ours(m.admin)
    if pre_users or pre_roles:
        print(f"refusing: hs-* / hs_* already exist: {pre_users} {pre_roles}")
        return 2
    print(f"before: {len(m.admin.list_users())} database users, "
          f"{len(m.admin.list_roles())} custom roles in the project (none hs-*/hs_*)")
    shared = get_client(None)
    db = shared[a.db]
    results: list[bool] = []
    stop = threading.Event()
    user = None
    try:
        perm = db.permissions.find_one({"_id": "safety"})
        print(f"safety permission: read={perm['read']} write={perm['write']}")
        t0 = time.monotonic()
        ident = m.provision("safety", a.db, perm, os.environ["MONGODB_URI"], wait_s=180)
        print(f"provisioned {ident.username} / {ident.role}: API calls "
              f"{time.monotonic() - t0 - ident.ready_s:.1f}s, then usable after "
              f"{ident.ready_s}s (total {time.monotonic() - t0:.1f}s)")
        user = MongoClient(ident.uri, serverSelectionTimeoutMS=10000)
        udb = user[a.db]
        print("as the generated user (raw pymongo, no ScopedDb):")
        attempt("read lane_status", lambda: list(udb.lane_status.find().limit(1)), True, results)
        attempt("read context", lambda: list(udb.context.find().limit(1)), True, results)
        attempt("insert intents_safety", lambda: udb.intents_safety.insert_one(intent("safety")),
                True, results)
        attempt("read transmissions", lambda: list(udb.transmissions.find().limit(1)), False,
                results)
        attempt("insert decisions", lambda: udb.decisions.insert_one({"t": datetime.now(UTC)}),
                False, results)
        attempt("insert intents_procedures",
                lambda: udb.intents_procedures.insert_one(intent("procedures")), False, results)

        # the read list changes in the permission document -> sync -> Atlas role follows
        th = threading.Thread(target=m.sync, args=(db, {"safety"}, stop), daemon=True)
        th.start()
        time.sleep(3)
        new_read = sorted(set(perm["read"]) | {"transmissions"})
        t1 = time.monotonic()
        db.permissions.update_one({"_id": "safety"}, {"$set": {"read": new_read}})
        role_t = None
        while time.monotonic() - t1 < 60:
            role = m.admin.get_role(role_name(a.db, "safety")) or {}
            colls = {r["collection"] for x in role.get("actions", []) if x["action"] == "FIND"
                     for r in x["resources"]}
            if "transmissions" in colls:
                role_t = time.monotonic() - t1
                break
            time.sleep(1)
        results.append(role_t is not None)
        print(f"  [{'PASS' if role_t is not None else 'FAIL'}] role has FIND on transmissions "
              f"after the read-list change: {role_t and round(role_t, 1)}s")
        enforced_t = None
        while time.monotonic() - t1 < 120:
            try:
                list(udb.transmissions.find().limit(1))
                enforced_t = time.monotonic() - t1
                break
            except OperationFailure:
                time.sleep(1)
        results.append(enforced_t is not None)
        print(f"  [{'PASS' if enforced_t is not None else 'FAIL'}] user can read transmissions "
              f"(Atlas applied the role): {enforced_t and round(enforced_t, 1)}s")
        stop.set()
        th.join(5)
        db.permissions.update_one({"_id": "safety"}, {"$set": {"read": perm["read"]}})
    except Exception as e:  # noqa: BLE001
        results.append(False)
        print(f"FAILED: {redact(e)}")
    finally:
        stop.set()
        if user is not None:
            user.close()
        gone = m.revoke("safety", a.db)
        print(f"revoked: {len(gone)} users, role {role_name(a.db, 'safety')}")
        left_u, left_r = ours(m.admin)
        clean = not left_u and not left_r
        results.append(clean)
        print(f"  [{'PASS' if clean else 'FAIL'}] GET after revoke: hs-* users {left_u}, "
              f"hs_* roles {left_r}")
        shared.drop_database(a.db)
        print(f"dropped database {a.db}")
        shared.close()
        m.admin.close()
    print("LIVE CHECK " + ("PASSED" if all(results) else "FAILED"))
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
