"""Working-memory writer: one ``context`` item per fact, retired on purpose.

Sources (change streams): ``transmissions`` and ``decisions`` and ``weather`` inserts, and
``objects`` deletes (removed or TTL-expired). Every function here is a plain call so tests
and other workers can use it without a change stream.

Kinds: transmission clearance -> clearance, request -> request, readback / report -> report,
stop -> alert, other -> note. Executed warn / stop decisions -> alert. Weather -> weather.

Retirement (``superseded_by`` set, ``expires_at`` = now + ``grace_s``):
- a newer instruction (clearance or stop) to the same callsign supersedes its older
  clearance items;
- a report that the callsign is clear of the runway / vacated / airborne supersedes its
  runway clearance;
- a new weather item supersedes the previous one;
- an object that is deleted or expires: its items expire in ``grace_s`` (no superseded_by).
``sweep()`` deletes everything past ``expires_at`` (the TTL monitor can lag a minute).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from pymongo.database import Database

from holdshort.contracts import HOLDSHORT, STATIONS, TOWER_ID, UNKNOWN_SPEAKER

LIFETIMES_S: dict[str, float] = {
    "clearance": 15 * 60,
    "request": 5 * 60,
    "report": 5 * 60,
    "alert": 10 * 60,
    "note": 2 * 60,
    "weather": 6 * 3600,  # only the newest stays: superseded by the next one
}
SUPERSEDED_GRACE_S = 60.0

TX_KIND = {
    "clearance": "clearance", "request": "request", "readback": "report",
    "report": "report", "stop": "alert", "other": "note",
}
RUNWAY_INSTRUCTIONS = ("land", "takeoff", "cross", "line_up_wait")
CLEAR_OF_RUNWAY = re.compile(r"\b(clear(ed)? of (the )?(runway|rwy)|vacated|airborne)\b", re.I)
NON_CALLSIGNS = set(STATIONS) | {UNKNOWN_SPEAKER, HOLDSHORT}
SUMMARY_MAX = 160


def _now() -> datetime:
    return datetime.now(UTC)


def _hms(t: datetime | None) -> str:
    return (t or _now()).astimezone(UTC).strftime("%H:%M:%S")


def _short_stream(stream_id: str | None) -> str:
    return (stream_id or "-").split("_")[-1]


def _one_line(s: str, n: int = SUMMARY_MAX) -> str:
    s = " ".join(str(s or "").split())
    return s if len(s) <= n else s[: n - 1] + "…"


def callsigns_of(*names: Any) -> list[str]:
    out: list[str] = []
    for n in names:
        if isinstance(n, list | tuple):
            out += [c for c in callsigns_of(*n) if c not in out]
        elif n and str(n) not in NON_CALLSIGNS and str(n) not in out:
            out.append(str(n))
    return out


@dataclass
class Writer:
    db: Database
    lifetimes: dict[str, float] = field(default_factory=lambda: dict(LIFETIMES_S))
    grace_s: float = SUPERSEDED_GRACE_S

    # ------------------------------------------------------------------ helpers

    def _expires(self, kind: str, t: datetime) -> datetime:
        # lifetimes count from when we learn the fact, so an old replayed event still lives
        return max(t, _now()) + timedelta(seconds=self.lifetimes.get(kind, 120))

    def _insert(self, item: dict[str, Any]) -> Any:
        return self.db.context.insert_one(item).inserted_id

    def supersede(self, query: dict[str, Any], by: Any) -> int:
        """Mark live items matching ``query`` superseded by ``by``; they expire in grace_s."""
        soon = _now() + timedelta(seconds=self.grace_s)
        q = {**query, "superseded_by": None, "_id": {"$ne": by}}
        r = self.db.context.update_many(q, [{"$set": {
            "superseded_by": by, "expires_at": {"$min": ["$expires_at", soon]}}}])
        return r.modified_count

    # ------------------------------------------------------------------ sources

    def from_transmission(self, tx: dict[str, Any]) -> Any:
        kind = TX_KIND.get(tx.get("kind") or "other", "note")
        if tx.get("flag") and str(tx.get("flag")).startswith("unclear"):
            kind = "note" if kind != "alert" else kind
        t = tx.get("t") or _now()
        instr = tx.get("instruction")
        instr = None if instr in (None, "none") else instr
        conf = tx.get("confidence")
        text = tx.get("transcript") or ""
        summary = (f"{_hms(t)} {_short_stream(tx.get('stream_id'))} "
                   f"{tx.get('from', '?')}->{tx.get('to', '?')} {text}")
        if conf is not None:
            summary = f"{_one_line(summary, SUMMARY_MAX - 24)} (conf {float(conf):.2f})"
        if tx.get("readback") in ("mismatch", "missing"):
            summary += f" [readback {tx['readback']}]"
        if tx.get("flag"):
            summary += f" [{tx['flag']}]"
        calls = callsigns_of(tx.get("to"), tx.get("from"))
        item = {
            "tower_id": tx.get("tower_id") or TOWER_ID, "kind": kind,
            "summary": _one_line(summary, SUMMARY_MAX + 40),
            "callsigns": calls, "lane_ids": [tx["lane_id"]] if tx.get("lane_id") else [],
            "stream_id": tx.get("stream_id"), "instruction": instr,
            "readback": tx.get("readback"), "flag": tx.get("flag"),
            "source": {"coll": "transmissions", "id": tx.get("_id")}, "t": t,
            "superseded_by": None, "expires_at": self._expires(kind, t),
        }
        new_id = self._insert(item)
        base = {"tower_id": item["tower_id"], "kind": "clearance", "t": {"$lte": t}}
        if calls and instr and tx.get("kind") in ("clearance", "stop"):
            self.supersede({**base, "callsigns": {"$in": calls}}, new_id)
        if calls and tx.get("kind") in ("report", "readback") and CLEAR_OF_RUNWAY.search(text):
            self.supersede({**base, "callsigns": {"$in": calls},
                            "instruction": {"$in": list(RUNWAY_INSTRUCTIONS)}}, new_id)
        return new_id

    def from_decision(self, d: dict[str, Any]) -> Any | None:
        if d.get("outcome") != "executed" or d.get("intent_action") not in ("warn", "stop"):
            return None
        t = d.get("t") or _now()
        target = callsigns_of(d.get("target") or [])
        summary = (f"{_hms(t)} ALERT {d.get('intent_action')} {d.get('severity', '')} "
                   f"{d.get('rule', '')} {d.get('lane_id') or ''} {','.join(target)}: "
                   f"{d.get('text') or ''}")
        return self._insert({
            "tower_id": d.get("tower_id") or TOWER_ID, "kind": "alert",
            "summary": _one_line(summary), "callsigns": target,
            "lane_ids": [d["lane_id"]] if d.get("lane_id") else [],
            "stream_id": d.get("stream_id"), "instruction": None, "readback": None,
            "flag": d.get("rule"), "source": {"coll": "decisions", "id": d.get("_id")},
            "t": t, "superseded_by": None, "expires_at": self._expires("alert", t),
        })

    def from_weather(self, w: dict[str, Any]) -> Any:
        t = w.get("t") or _now()
        tower = w.get("tower_id") or TOWER_ID
        summary = (f"{_hms(t)} WX vis {w.get('visibility_sm')} sm wind {w.get('wind') or '-'}"
                   + (f" {w['raw']}" if w.get("raw") else ""))
        new_id = self._insert({
            "tower_id": tower, "kind": "weather", "summary": _one_line(summary),
            "callsigns": [], "lane_ids": [], "stream_id": None, "instruction": None,
            "readback": None, "flag": None, "source": {"coll": "weather", "id": w.get("_id")},
            "t": t, "superseded_by": None, "expires_at": self._expires("weather", t),
        })
        self.supersede({"tower_id": tower, "kind": "weather", "t": {"$lte": t}}, new_id)
        return new_id

    def object_gone(self, object_id: str) -> int:
        """The object was deleted or expired: its live items expire within grace_s."""
        soon = _now() + timedelta(seconds=self.grace_s)
        r = self.db.context.update_many(
            {"callsigns": str(object_id), "expires_at": {"$gt": soon}},
            {"$set": {"expires_at": soon}})
        return r.modified_count

    def sweep(self) -> int:
        """Delete every item past expires_at (the TTL monitor can lag ~60 s)."""
        return self.db.context.delete_many({"expires_at": {"$lte": _now()}}).deleted_count
