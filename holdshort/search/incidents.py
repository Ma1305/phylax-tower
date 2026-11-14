"""Find past runway incidents similar to a situation.

    similar("fire truck crossing active runway during landing", k=3)

Uses Vector Search with automated embedding (Voyage, index `incidents_vector` on
`summary`) when that index is queryable, otherwise falls back to the Atlas
Search text index `incidents_text`.
"""

from __future__ import annotations

from dataclasses import dataclass

from pymongo.database import Database

from holdshort.db import get_db

COLLECTION = "incidents"
VECTOR_INDEX = "incidents_vector"
TEXT_INDEX = "incidents_text"
EMBED_MODEL = "voyage-4"

VECTOR_DEFINITION = {
    "fields": [
        {"type": "autoEmbed", "modality": "text", "path": "summary", "model": EMBED_MODEL},
        {"type": "filter", "path": "airport"},
    ]
}
TEXT_DEFINITION = {
    "mappings": {
        "dynamic": False,
        "fields": {
            "title": {"type": "string", "analyzer": "lucene.english"},
            "summary": {"type": "string", "analyzer": "lucene.english"},
            "tags": {"type": "string", "analyzer": "lucene.english"},
            "airport": {"type": "token"},
        },
    }
}

_PROJECT = {"title": 1, "airport": 1, "date": 1, "summary": 1, "source": 1}


@dataclass(frozen=True)
class Incident:
    id: str
    title: str
    airport: str
    date: str
    summary: str
    source: str
    score: float
    method: str  # "vector" or "text"


def _queryable(db: Database, name: str) -> bool:
    for ix in db[COLLECTION].list_search_indexes(name):
        return bool(ix.get("queryable"))
    return False


def _vector(db: Database, text: str, k: int) -> list[dict]:
    return list(
        db[COLLECTION].aggregate(
            [
                {
                    "$vectorSearch": {
                        "index": VECTOR_INDEX,
                        "path": "summary",
                        "query": text,
                        "numCandidates": max(50, k * 20),
                        "limit": k,
                    }
                },
                {"$project": {**_PROJECT, "score": {"$meta": "vectorSearchScore"}}},
            ]
        )
    )


def _text(db: Database, text: str, k: int) -> list[dict]:
    return list(
        db[COLLECTION].aggregate(
            [
                {
                    "$search": {
                        "index": TEXT_INDEX,
                        "compound": {
                            "should": [
                                {"text": {"query": text, "path": "summary"}},
                                {
                                    "text": {
                                        "query": text,
                                        "path": "tags",
                                        "score": {"boost": {"value": 2}},
                                    }
                                },
                                {"text": {"query": text, "path": "title"}},
                            ]
                        },
                    }
                },
                {"$limit": k},
                {"$project": {**_PROJECT, "score": {"$meta": "searchScore"}}},
            ]
        )
    )


def similar(
    text: str, k: int = 3, db: Database | None = None, method: str | None = None
) -> list[Incident]:
    """Top-k incidents most like `text`. method: "vector", "text", or None (vector if ready)."""
    db = db if db is not None else get_db()
    if method is None:
        method = "vector" if _queryable(db, VECTOR_INDEX) else "text"
    docs = _vector(db, text, k) if method == "vector" else _text(db, text, k)
    return [
        Incident(
            d["_id"],
            d["title"],
            d["airport"],
            d["date"],
            d["summary"],
            d.get("source", ""),
            float(d["score"]),
            method,
        )
        for d in docs
    ]
