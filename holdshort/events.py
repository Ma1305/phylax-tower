"""state_events: every change to current state the timeline must be able to rebuild."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from pymongo.database import Database


def log_state_event(db: Database, kind: str, ref: str, doc: dict[str, Any] | None,
                    by: str | None = None, tower_id: str = "LGA",
                    t: datetime | None = None) -> Any:
    """Insert one state_events document; returns its _id.

    kind: lane_status | permission | controller | rule_set | object_removed.
    doc: the full document after the change (None for object_removed is allowed).
    """
    res = db.state_events.insert_one({
        "t": t or datetime.now(UTC), "tower_id": tower_id, "kind": kind, "ref": str(ref),
        "doc": doc, "by": by,
    })
    return res.inserted_id
