"""Per-agent database handles limited to the agent's permission read/write lists.

    scoped = scoped_db(db, "safety")
    scoped.context.find({...})          # allowed: context is in safety.read
    scoped.intents_safety.insert_one()  # allowed: in safety.write
    scoped.decisions.insert_one({})     # AccessDenied + a security_events document

A denied call raises ``AccessDenied`` and inserts a ``security_events`` document
(kind read_denied / write_denied) through the unscoped database. The lists are reloaded
from ``permissions`` at most every ``RELOAD_S`` seconds.

``connect_for(agent)`` connects as a generated per-agent Atlas user when Atlas admin
credentials are set (holdshort/identity), else ``MONGODB_URI_<AGENT>``, else ``MONGODB_URI``.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from datetime import UTC, datetime
from typing import Any

from pymongo import MongoClient
from pymongo.database import Database

from holdshort.db import get_client

RELOAD_S = 5.0
log = logging.getLogger("holdshort.access")

READ_METHODS = frozenset({
    "find", "find_one", "aggregate", "count_documents", "estimated_document_count",
    "distinct", "watch", "find_raw_batches", "aggregate_raw_batches",
})
WRITE_METHODS = frozenset({
    "insert_one", "insert_many", "update_one", "update_many", "replace_one", "delete_one",
    "delete_many", "find_one_and_update", "find_one_and_replace", "find_one_and_delete",
    "bulk_write",
})
PASSTHROUGH = frozenset({"name", "full_name"})


class AccessDenied(PermissionError):
    """An agent tried to use a collection its permission does not allow."""

    def __init__(self, agent: str, kind: str, attempted: str):
        super().__init__(f"{agent}: {kind} ({attempted})")
        self.agent = agent
        self.kind = kind
        self.attempted = attempted


class ScopedDb:
    """A database handle for one agent. ``scoped["name"]`` / ``scoped.name`` -> proxy."""

    def __init__(self, db: Database, agent: str):
        self._db = db
        self.agent = agent
        self._lock = threading.Lock()
        self._loaded_at = 0.0
        self._read: frozenset[str] = frozenset()
        self._write: frozenset[str] = frozenset()

    @property
    def unscoped_name(self) -> str:
        return self._db.name

    @property
    def name(self) -> str:  # database name, like pymongo
        return self._db.name

    def lists(self) -> tuple[frozenset[str], frozenset[str]]:
        """(read, write) collection names, reloaded at most every RELOAD_S seconds."""
        with self._lock:
            if time.monotonic() - self._loaded_at >= RELOAD_S:
                p = self._db.permissions.find_one({"_id": self.agent}) or {}
                self._write = frozenset(p.get("write") or [])
                self._read = frozenset(p.get("read") or []) | self._write
                self._loaded_at = time.monotonic()
            return self._read, self._write

    def reload(self) -> None:
        with self._lock:
            self._loaded_at = 0.0

    def check(self, coll: str, method: str) -> None:
        read, write = self.lists()
        if method in WRITE_METHODS:
            allowed, kind = coll in write, "write_denied"
        else:
            allowed, kind = coll in read, "read_denied"
        if allowed:
            return
        attempted = f"{coll}.{method}"
        try:
            self._db.security_events.insert_one({
                "t": datetime.now(UTC), "agent": self.agent, "kind": kind,
                "attempted": attempted, "error_code": None,
                "error": f"{coll} is not in the {self.agent} {kind.split('_')[0]} list",
            })
        finally:
            raise AccessDenied(self.agent, kind, attempted)

    def __getitem__(self, name: str) -> ScopedCollection:
        return ScopedCollection(self, name)

    def __getattr__(self, name: str) -> ScopedCollection:
        if name.startswith("_"):
            raise AttributeError(name)
        return ScopedCollection(self, name)

    def __repr__(self) -> str:
        return f"ScopedDb({self._db.name!r}, agent={self.agent!r})"


class ScopedCollection:
    """A collection proxy that only exposes read/write methods the agent may use."""

    def __init__(self, scoped: ScopedDb, name: str):
        self._scoped = scoped
        self._name = name
        self._coll = scoped._db[name]

    def __getattr__(self, attr: str) -> Any:
        if attr in PASSTHROUGH:
            return getattr(self._coll, attr)
        if attr not in READ_METHODS and attr not in WRITE_METHODS:
            raise AttributeError(f"{attr} is not available on a scoped collection")
        target = getattr(self._coll, attr)

        def call(*args: Any, **kwargs: Any) -> Any:
            self._scoped.check(self._name, attr)
            return target(*args, **kwargs)

        return call

    def __repr__(self) -> str:
        return f"ScopedCollection({self._name!r}, agent={self._scoped.agent!r})"


_scoped: dict[tuple, ScopedDb] = {}
_scoped_lock = threading.Lock()


def scoped_db(db: Database, agent: str) -> ScopedDb:
    """The (shared) ScopedDb for this database and agent."""
    key = (id(db.client), db.name, agent)
    with _scoped_lock:
        s = _scoped.get(key)
        if s is None:
            s = _scoped[key] = ScopedDb(db, agent)
        return s


_identity_logged = False


def connect_for(agent: str, *, process: str | None = None, db: str | None = None,
                **kwargs: Any) -> MongoClient:
    """A client for this agent: a generated Atlas user when admin credentials are set
    (holdshort/identity), else MONGODB_URI_<AGENT>, else MONGODB_URI. The choice is recorded
    in agent_state.<process>.identity (never the password)."""
    global _identity_logged
    from holdshort.env import mongodb_db_name
    from holdshort.identity import Identity, generated_identity, missing

    db_name = db or mongodb_db_name()
    shared = get_client(None, **kwargs)
    ident = None
    if not missing():
        try:
            ident = generated_identity(agent, db_name, shared[db_name])
        except Exception as e:  # noqa: BLE001  (never fail a process over identity)
            log.warning("identity: generated user for %s failed (%s), falling back",
                        agent, type(e).__name__)
    elif not _identity_logged:
        _identity_logged = True
        log.info("identity: Atlas admin credentials not set, agents use the shared "
                 "connection (or MONGODB_URI_<AGENT>)")
    if ident is None:
        static = os.environ.get(f"MONGODB_URI_{agent.upper()}", "").strip()
        ident = Identity("static", static) if static else Identity("shared")
    try:
        shared[db_name].agent_state.update_one(
            {"_id": process or agent},
            {"$set": {"identity": ident.public(), "updated_at": datetime.now(UTC)}},
            upsert=True)
    except Exception as e:  # noqa: BLE001
        log.warning("identity: could not record identity for %s (%s)", agent, type(e).__name__)
    if ident.uri is None:
        return shared
    shared.close()
    return get_client(ident.uri, **kwargs)
