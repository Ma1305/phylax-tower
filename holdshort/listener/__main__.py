"""python -m holdshort.listener [--db NAME] [--workers 8]

Watches audio_clips for inbound queued clips on every stream and turns each one into a
Transmission (see holdshort/listener/service.py). Exits cleanly on SIGTERM / Ctrl-C.
"""

from __future__ import annotations

import argparse
import logging
import signal
import sys

from holdshort.db import get_client
from holdshort.env import mongodb_db_name, redact
from holdshort.listener.service import MultiStreamListener


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m holdshort.listener")
    parser.add_argument("--db", default=None, help="database (default: MONGODB_DB)")
    parser.add_argument("--workers", type=int, default=8, help="parallel streams (default 8)")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    client = get_client()
    try:
        agent = MultiStreamListener(client[args.db or mongodb_db_name()], workers=args.workers)
        signal.signal(signal.SIGTERM, lambda *_: agent.stop())
        try:
            agent.run()
        except KeyboardInterrupt:
            agent.stop()
        return 0
    except Exception as e:  # never leak the connection string
        print(f"FAIL: {type(e).__name__}: {redact(e)}", file=sys.stderr)
        return 1
    finally:
        client.close()


if __name__ == "__main__":
    sys.exit(main())
