"""JSON-safe conversion of MongoDB documents (ObjectId -> str, datetime -> ISO 8601 Z)."""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any

from bson import ObjectId


def iso(dt: datetime) -> str:
    """ISO 8601 in UTC with a Z suffix (naive datetimes are taken as UTC)."""
    dt = dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)
    spec = "milliseconds" if dt.microsecond else "seconds"
    return dt.isoformat(timespec=spec).replace("+00:00", "Z")


def jsonable(obj: Any) -> Any:
    """Recursively convert ObjectId -> str and datetime -> ISO 8601 Z."""
    if isinstance(obj, dict):
        return {str(k): jsonable(v) for k, v in obj.items()}
    if isinstance(obj, list | tuple):
        return [jsonable(v) for v in obj]
    if isinstance(obj, ObjectId):
        return str(obj)
    if isinstance(obj, datetime):
        return iso(obj)
    if isinstance(obj, date):
        return obj.isoformat()
    return obj
