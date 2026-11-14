"""CLI for the action gate: run it, or approve / reject / revoke / set-permission."""

from __future__ import annotations

import argparse
import logging
import signal
import sys

from holdshort.db import get_client
from holdshort.env import mongodb_db_name, redact
from holdshort.gate import Gate, GateError, approve, reject, revoke, set_permission


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m holdshort.gate", description="Hold Short action gate"
    )
    parser.add_argument("--db", default=None, help="database (default: MONGODB_DB)")
    sub = parser.add_subparsers(dest="cmd")
    p = sub.add_parser("approve", help="approve a pending decision")
    p.add_argument("decision_id")
    p.add_argument("controller")
    p = sub.add_parser("reject", help="reject a pending decision")
    p.add_argument("decision_id")
    p.add_argument("controller")
    p.add_argument("reason", nargs="?")
    p = sub.add_parser("revoke", help="revoke a decision")
    p.add_argument("decision_id")
    p.add_argument("controller")
    p = sub.add_parser("set-permission", help="change an agent's switch, mode or tier")
    p.add_argument("agent")
    p.add_argument("by")
    p.add_argument("--enabled", choices=["on", "off"])
    p.add_argument("--mode", choices=["reactive", "proactive"])
    p.add_argument("--tier", choices=["autonomous", "needs_approval", "never"])
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    client = get_client()
    db = client[args.db or mongodb_db_name()]
    try:
        if args.cmd == "approve":
            print(approve(db, args.decision_id, args.controller)["_id"])
        elif args.cmd == "reject":
            print(reject(db, args.decision_id, args.controller, args.reason)["_id"])
        elif args.cmd == "revoke":
            print(revoke(db, args.decision_id, args.controller)["_id"])
        elif args.cmd == "set-permission":
            enabled = None if args.enabled is None else args.enabled == "on"
            doc = set_permission(db, args.agent, args.by, enabled=enabled, mode=args.mode,
                                 tier=args.tier)
            print(f"{doc['_id']}: enabled={doc['enabled']} mode={doc['mode']} tier={doc['tier']}")
        else:
            gate = Gate(db)
            signal.signal(signal.SIGTERM, lambda *_: gate.stop())
            try:
                gate.run()
            except KeyboardInterrupt:
                pass
        return 0
    except GateError as e:
        print(f"ERROR: {e.code}: {e.detail}", file=sys.stderr)
        return 1
    except Exception as e:  # never leak the connection string
        print(f"FAIL: {type(e).__name__}: {redact(e)}", file=sys.stderr)
        return 1
    finally:
        client.close()


if __name__ == "__main__":
    sys.exit(main())
