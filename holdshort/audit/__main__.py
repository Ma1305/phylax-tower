"""Ask the audit agent a question: uv run python -m holdshort.audit "Why was TRUCK1 held?" """

import argparse
import json
import sys

from holdshort.audit import explain


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.strip())
    p.add_argument("question")
    p.add_argument("--db", default=None, help="database name (default: MONGODB_DB)")
    p.add_argument("--json", action="store_true", help="print the full result as JSON")
    args = p.parse_args(argv)
    from holdshort.db import get_db

    out = explain(args.question, db=get_db(args.db))
    if args.json:
        print(json.dumps(out, default=str, indent=2))
    else:
        for it in out["timeline"]:
            print(f"{it['t'].isoformat()}  {it['kind']:<14} {it['summary']}  [{it['doc_id']}]")
        print()
        print(out["narrative"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
