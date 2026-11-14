"""Ask the audit agent a question: uv run python -m holdshort.audit "Why was TRUCK1 held?" """

import argparse
import json
import sys

from holdshort.audit.core import _json, explain


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.strip())
    p.add_argument("question")
    p.add_argument("--db", default=None, help="database name (default: MONGODB_DB)")
    p.add_argument("--tower", default="LGA")
    p.add_argument("--json", action="store_true", help="print the full result as JSON")
    args = p.parse_args(argv)
    from holdshort.db import get_db

    out = explain(get_db(args.db), args.question, tower_id=args.tower)
    if args.json:
        print(json.dumps(_json(out), indent=2))
        return 0
    print(out["narrative"])
    print()
    for it in out["timeline"]:
        print(f"{it['t'].isoformat()}  {it['kind']:<14} {it['summary']}  [{it['doc_id']}]")
    return 0


if __name__ == "__main__":
    sys.exit(main())
