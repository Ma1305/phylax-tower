"""Resolve a heard callsign to a known one with fuzzy Atlas Search, and flag confusable pairs.

    resolve("truck one")            -> Match(callsign="TRUCK1", score=..., ...)
    similar_on_frequency("AC8646")  -> [Confusable(callsign="AC8464", reason=...), ...]

Collection `callsigns` (seeded by scripts/setup_search.py):
    {_id: "TRUCK1", callsign: "TRUCK1", spoken: "truck one", kind: "vehicle",
     frequency: "ground"}
Search index `callsigns_search`: `callsign` uses a keyword+lowercase analyzer (the
whole callsign is one token, so fuzzy edits count characters), `spoken` uses the
standard analyzer (fuzzy per spoken word).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from pymongo.database import Database

from holdshort.db import get_db

COLLECTION = "callsigns"
INDEX = "callsigns_search"

INDEX_DEFINITION = {
    "analyzers": [
        {
            "name": "callsign_keyword",
            "tokenizer": {"type": "keyword"},
            "tokenFilters": [{"type": "lowercase"}],
        }
    ],
    "mappings": {
        "dynamic": False,
        "fields": {
            "callsign": {"type": "string", "analyzer": "callsign_keyword"},
            "spoken": {"type": "string", "analyzer": "lucene.standard"},
            "frequency": {"type": "token"},
            "kind": {"type": "token"},
        },
    },
}

# Spoken radio words -> the characters they stand for.
_DIGITS = {
    "zero": "0",
    "one": "1",
    "two": "2",
    "three": "3",
    "tree": "3",
    "four": "4",
    "fower": "4",
    "five": "5",
    "fife": "5",
    "six": "6",
    "seven": "7",
    "eight": "8",
    "nine": "9",
    "niner": "9",
}
_NATO = {
    w: w[0].upper()
    for w in (
        "alpha alfa bravo charlie delta echo foxtrot golf hotel india juliet juliett kilo "
        "lima mike november oscar papa quebec romeo sierra tango uniform victor whiskey "
        "xray yankee zulu"
    ).split()
}
# Airline telephony (spoken company name -> callsign prefix). Checked before NATO
# letters, so "delta two two one four" means DL2214, not D2214.
_TELEPHONY = {"air canada": "AC", "jazz": "AC", "delta": "DL", "united": "UA"}


def normalize(heard: str) -> str:
    """Turn what was heard ("truck one", "air canada eight six four six") into callsign form."""
    text = re.sub(r"[^a-z0-9 ]+", " ", heard.lower()).strip()
    for spoken, prefix in _TELEPHONY.items():
        text = re.sub(rf"\b{spoken}\b", f" {prefix.lower()} ", text)
    out = []
    for word in text.split():
        if word in _DIGITS:
            out.append(_DIGITS[word])
        elif word in _NATO and len(text.split()) > 1:
            out.append(_NATO[word])
        else:
            out.append(word.upper())
    return "".join(out)


@dataclass(frozen=True)
class Match:
    callsign: str
    score: float
    kind: str | None = None
    frequency: str | None = None


@dataclass(frozen=True)
class Confusable:
    callsign: str
    other: str
    score: float
    reason: str


def _search(db: Database, compound: dict, limit: int, extra: list[dict] | None = None) -> list:
    pipeline = [
        {"$search": {"index": INDEX, "compound": compound}},
        *(extra or []),
        {"$limit": limit},
        {"$project": {"callsign": 1, "kind": 1, "frequency": 1, "score": {"$meta": "searchScore"}}},
    ]
    return list(db[COLLECTION].aggregate(pipeline))


def candidates(heard: str, limit: int = 5, db: Database | None = None) -> list[Match]:
    """Ranked known callsigns for a heard string (best first)."""
    db = db if db is not None else get_db()
    norm = normalize(heard)
    should = [
        # exact callsign form wins outright
        {"text": {"query": norm, "path": "callsign", "score": {"boost": {"value": 5}}}},
        {"text": {"query": norm, "path": "callsign", "fuzzy": {"maxEdits": 2, "prefixLength": 0}}},
        {"text": {"query": heard, "path": "spoken", "fuzzy": {"maxEdits": 1}}},
    ]
    docs = _search(db, {"should": should, "minimumShouldMatch": 1}, limit)
    return [
        Match(d["callsign"], float(d["score"]), d.get("kind"), d.get("frequency")) for d in docs
    ]


def resolve(heard: str, db: Database | None = None) -> Match | None:
    """Best known callsign for what was heard, with its search score; None if nothing is close."""
    found = candidates(heard, limit=1, db=db)
    return found[0] if found else None


def _reason(a: str, b: str) -> str:
    da, db_ = re.sub(r"\D", "", a), re.sub(r"\D", "", b)
    pa, pb = re.sub(r"\d", "", a), re.sub(r"\d", "", b)
    if pa == pb and da and sorted(da) == sorted(db_):
        return "same prefix, same digits in a different order"
    if pa == pb and len(da) == len(db_):
        diff = sum(x != y for x, y in zip(da, db_, strict=True))
        return f"same prefix, {diff} digit(s) different"
    return "similar spelling"


def similar_on_frequency(
    callsign: str, frequency: str | None = None, db: Database | None = None, limit: int = 5
) -> list[Confusable]:
    """Other callsigns on the same frequency that are easy to confuse with `callsign`.

    The frequency defaults to the one recorded for `callsign`; pass "any" to check all.
    """
    db = db if db is not None else get_db()
    callsign = callsign.upper()
    if frequency is None:
        doc = db[COLLECTION].find_one({"_id": callsign}, {"frequency": 1})
        frequency = doc.get("frequency") if doc else None
    compound: dict = {
        "must": [
            {
                "text": {
                    "query": callsign,
                    "path": "callsign",
                    "fuzzy": {"maxEdits": 2, "prefixLength": 1},
                }
            }
        ],
    }
    if frequency and frequency != "any":
        compound["filter"] = [{"equals": {"path": "frequency", "value": frequency}}]
    docs = _search(db, compound, limit, [{"$match": {"_id": {"$ne": callsign}}}])
    return [
        Confusable(callsign, d["callsign"], float(d["score"]), _reason(callsign, d["callsign"]))
        for d in docs
    ]
