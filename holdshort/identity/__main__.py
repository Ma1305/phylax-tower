"""python -m holdshort.identity status|provision|revoke|sync [--agent NAME | --all] [--db NAME]

provision and sync need AGENT_IDENTITIES=generated (nothing creates Atlas users implicitly);
revoke works without the switch so an operator can always clean up."""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor

import holdshort.env  # noqa: F401  (loads .env)
from holdshort.env import mongodb_db_name, redact
from holdshort.identity.atlas import credentials, enabled, missing, mode
from holdshort.identity.core import (
    PING_TIMEOUT_S,
    IdentityManager,
    NotReady,
    role_name,
    role_payload,
    ttl_hours,
    wait_s,
)

AGENTS = ("listener", "safety", "procedures", "weather")


def _shared_db(name: str):
    from holdshort.db import get_db

    return get_db(name)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m holdshort.identity")
    ap.add_argument("command", choices=["status", "provision", "revoke", "sync"])
    ap.add_argument("--agent", choices=AGENTS)
    ap.add_argument("--all", action="store_true", help="every agent (provision / revoke)")
    ap.add_argument("--db", default=None)
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stdout)
    logging.getLogger("httpx").setLevel(logging.WARNING)  # request lines carry the project id
    db = a.db or mongodb_db_name()
    agents = [a.agent] if a.agent else list(AGENTS)
    gaps = missing()

    if a.command == "status":
        print(f"database: {db}")
        print(f"AGENT_IDENTITIES: {mode()}  (generated identities are used only when "
              "this is 'generated')")
        print(f"user TTL: {ttl_hours():g} h; startup wait AGENT_IDENTITY_WAIT_S: {wait_s():g} s")
        for g in gaps:
            print(f"  missing: {g}")
        if not gaps:
            c = credentials()
            print(f"  Atlas admin credentials: set ({c.kind}, cluster {c.cluster})")
        on = enabled() and not gaps
        print("generated identities: " + ("ON" if on else
              "OFF (agents use MONGODB_URI_<AGENT> or the shared connection)"))
        for ag in agents:
            static = "set" if os.environ.get(f"MONGODB_URI_{ag.upper()}", "").strip() else "-"
            print(f"  {ag:<11} role {role_name(db, ag):<28} MONGODB_URI_{ag.upper()}: {static}")
        if on and os.environ.get("MONGODB_URI", "").strip():
            for ag in agents:
                perm = _shared_db(db).permissions.find_one({"_id": ag}) or {}
                acts = role_payload(db, ag, perm)["actions"]
                print(f"  {ag}: " + ", ".join(
                    f"{x['action']}({len(x['resources'])})" for x in acts))
        return 0

    if a.command in ("provision", "revoke") and not (a.agent or a.all):
        print(f"{a.command}: pass --agent NAME or --all")
        return 2
    if gaps:
        print("BLOCKED: generated identities need " + "; ".join(gaps))
        return 2
    if a.command != "revoke" and not enabled():
        print("BLOCKED: set AGENT_IDENTITIES=generated to create Atlas users")
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
        # create every role + user first, then wait for all of them together (Atlas applies
        # queued changes in one batch; one-by-one waiting multiplies the delay)
        made = {}
        for ag in agents:
            perm = shared.permissions.find_one({"_id": ag}) or {}
            try:
                made[ag] = m.provision(ag, db, perm, os.environ["MONGODB_URI"], wait_s=0)
            except NotReady as e:
                made[ag] = e.identity
        t0 = time.monotonic()
        with ThreadPoolExecutor(len(made) or 1) as pool:
            futs = {ag: pool.submit(m.wait_usable, ident,
                                    max(0.0, PING_TIMEOUT_S - (time.monotonic() - t0)))
                    for ag, ident in made.items()}
        rc = 0
        for ag, ident in made.items():
            if futs[ag].exception() is None:
                print(f"{ag}: user {ident.username} role {ident.role} usable after "
                      f"{ident.ready_s}s, expires {ident.expires_at.isoformat()}")
            else:
                rc = 1
                print(f"{ag}: user {ident.username} NOT usable within {PING_TIMEOUT_S:g}s")
        return rc
    except KeyboardInterrupt:
        return 0
    except Exception as e:  # noqa: BLE001
        print(f"FAILED: {redact(e)}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
