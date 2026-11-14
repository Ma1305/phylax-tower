"""MongoDB client helpers. Clients are tz-aware so BSON dates come back as UTC datetimes."""

from pymongo import MongoClient
from pymongo.database import Database

from holdshort.env import URI_HELP, mongodb_db_name, require


def get_client(uri: str | None = None, **kwargs) -> MongoClient:
    """Return a tz-aware MongoClient for MONGODB_URI (exits BLOCKED if unset)."""
    kwargs.setdefault("serverSelectionTimeoutMS", 15000)
    kwargs.setdefault("tz_aware", True)
    return MongoClient(uri or require("MONGODB_URI", URI_HELP), **kwargs)


def get_db(name: str | None = None, client: MongoClient | None = None) -> Database:
    """Return the Phylax Tower database (MONGODB_DB, default "holdshort")."""
    return (client or get_client())[name or mongodb_db_name()]
