"""python -m holdshort.identity status|provision|revoke|sync [--agent NAME] [--db NAME]"""

from __future__ import annotations

import argparse
import logging
import os
import sys

import holdshort.env  # noqa: F401  (loads .env)
from holdshort.env import mongodb_db_name, redact
from holdshort.identity.atlas import credentials, missing
from holdshort.identity.core import IdentityManager, role_name, role_payload, ttl_hours

AGENTS = ("listener", "safety", "procedures", "weather")


def _shared_db(name: str):
    from holdshort.db import get_db

    return get_db(name)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m holdshort.identity")
    ap.add_argument("command", choices=["status", "provision", "revoke", "sync"])
    ap.add_argument("--agent", choices=AGENTS)
    ap.add_argument("--db", default=None)
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stdout)
    db = a.db or mongodb_db_name()
    agents = [a.agent] if a.agent else list(AGENTS)
    gaps = missing()

    if a.command == "status":
        print(f"database: {db}")
        print(f"user TTL: {ttl_hours():g} h")
        if gaps:
            print("generated identities: OFF (agents use the shared connection)")
            for g in gaps:
                print(f"  missing: {g}")
        else:
            c = credentials()
            print(f"generated identities: ON ({c.kind}, cluster {c.cluster})")
        for ag in agents:
            static = "set" if os.environ.get(f"MONGODB_URI_{ag.upper()}", "").strip() else "-"
            print(f"  {ag:<11} role {role_name(db, ag):<28} MONGODB_URI_{ag.upper()}: {static}")
        if not gaps and os.environ.get("MONGODB_URI", "").strip():
            for ag in agents:
                perm = _shared_db(db).permissions.find_one({"_id": ag}) or {}
                acts = role_payload(db, ag, perm)["actions"]
                print(f"  {ag}: " + ", ".join(
                    f"{x['action']}({len(x['resources'])})" for x in acts))
        return 0

    if gaps:
        print("BLOCKED: generated identities need " + "; ".join(gaps))
        return 2
    m = IdentityManager(credentials())
    try:
        if a.command == "revoke":
            for ag in agents:
                print(f"{ag}: deleted {len(m.revoke(ag, db))} users and role {role_name(db, ag)}")
            return 0
        shared = _shared_db(db)
        if a.command == "sync":
            m.sync(shared, set(agents))
            return 0
        for ag in agents:
            perm = shared.permissions.find_one({"_id": ag}) or {}
            ident = m.provision(ag, db, perm, os.environ["MONGODB_URI"])
            print(f"{ag}: user {ident.username} role {ident.role} expires "
                  f"{ident.expires_at.isoformat()}")
        return 0
    except KeyboardInterrupt:
        return 0
    except Exception as e:  # noqa: BLE001
        print(f"FAILED: {redact(e)}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
