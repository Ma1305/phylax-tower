"""CLI for the action gate: run it, or approve / revoke / set-permission."""

from __future__ import annotations

import argparse
import logging
import signal
import sys

from holdshort.db import get_client
from holdshort.env import mongodb_db_name, redact
from holdshort.gate import Gate, GateError, approve, revoke, set_permission


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m holdshort.gate", description="Hold Short action gate"
    )
    parser.add_argument("--db", default=None, help="database (default: MONGODB_DB)")
    sub = parser.add_subparsers(dest="cmd")
    p = sub.add_parser("approve", help="approve a pending intent")
    p.add_argument("intent_id")
    p.add_argument("controller")
    p = sub.add_parser("revoke", help="revoke a decision")
    p.add_argument("decision_id")
    p.add_argument("controller")
    p = sub.add_parser("set-permission", help="flip an agent or the system switch")
    p.add_argument("agent")
    p.add_argument("state", choices=["on", "off"])
    p.add_argument("by")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    client = get_client()
    db = client[args.db or mongodb_db_name()]
    try:
        if args.cmd == "approve":
            print(approve(db, args.intent_id, args.controller)["_id"])
        elif args.cmd == "revoke":
            print(revoke(db, args.decision_id, args.controller)["_id"])
        elif args.cmd == "set-permission":
            set_permission(db, args.agent, args.state == "on", args.by)
        else:
            gate = Gate(db)
            signal.signal(signal.SIGTERM, lambda *_: gate.stop())
            try:
                gate.run()
            except KeyboardInterrupt:
                pass
        return 0
    except GateError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    except Exception as e:  # never leak the connection string
        print(f"FAIL: {type(e).__name__}: {redact(e)}", file=sys.stderr)
        return 1
    finally:
        client.close()


if __name__ == "__main__":
    sys.exit(main())
