"""Per-agent Atlas identities generated from the agent's permission document.

provision(agent) -> Identity: custom role ``hs_<db>_<agent>`` + database user
``hs-<db>-<agent>-<rand>`` bound to that role only, scoped to the cluster, expiring after
AGENT_USER_TTL_HOURS. revoke(agent) deletes both. sync() keeps the role in step with
``permissions`` (change stream). Secrets (keys, generated passwords, URIs) are never logged.
"""

from __future__ import annotations

import logging
import os
import secrets
import string
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import quote_plus, urlsplit, urlunsplit

from holdshort.identity.atlas import AtlasAdmin, AtlasError, Credentials, credentials

log = logging.getLogger("holdshort.identity")

# Collections an agent may update / remove in, beyond insert (CONTRACTS.md: the listener
# updates audio_clips.status / transmission_id / error). Everything else is insert-only.
UPDATES: dict[str, frozenset[str]] = {"listener": frozenset({"audio_clips"})}

# Plumbing every worker needs to run at all; see README "Infrastructure grants".
# agent_state: resume tokens + identity; security_events: ScopedDb logs denied calls;
# permissions: ScopedDb reloads the agent's own read / write lists.
INFRA: dict[str, tuple[str, ...]] = {
    "agent_state": ("FIND", "INSERT", "UPDATE"),
    "security_events": ("INSERT",),
    "permissions": ("FIND",),
}

PING_TIMEOUT_S = 90.0
ALPHABET = string.ascii_letters + string.digits


def role_name(db: str, agent: str) -> str:
    return f"hs_{db}_{agent}"


def user_prefix(db: str, agent: str) -> str:
    return f"hs-{db}-{agent}-"


def ttl_hours() -> float:
    try:
        return float(os.environ.get("AGENT_USER_TTL_HOURS", "") or 6)
    except ValueError:
        return 6.0


def role_payload(db: str, agent: str, perm: dict, infra: bool = True) -> dict:
    """The Atlas custom-role body for one permission document (database ``db`` only)."""
    write = set(perm.get("write") or [])
    read = set(perm.get("read") or []) | write
    grants: dict[str, set[str]] = {}
    for c in read:
        grants.setdefault(c, set()).update({"FIND", "CHANGE_STREAM"})
    for c in write:
        grants[c].add("INSERT")
    for c in UPDATES.get(agent, frozenset()) & write:
        grants[c].update({"UPDATE", "REMOVE"})
    if infra:
        for c, acts in INFRA.items():
            grants.setdefault(c, set()).update(acts)
    by_action: dict[str, list[str]] = {}
    for c in sorted(grants):
        for a in sorted(grants[c]):
            by_action.setdefault(a, []).append(c)
    return {
        "roleName": role_name(db, agent),
        "actions": [
            {"action": a, "resources": [{"cluster": False, "db": db, "collection": c}
                                        for c in colls]}
            for a, colls in sorted(by_action.items())
        ],
        "inheritedRoles": [],
    }


def new_password() -> str:
    return "".join(secrets.choice(ALPHABET) for _ in range(32))


def user_uri(base_uri: str, username: str, password: str) -> str:
    """MONGODB_URI's host and options with this user's credentials (authSource=admin)."""
    p = urlsplit(base_uri)
    host = p.netloc.rsplit("@", 1)[-1]
    query = "&".join(q for q in p.query.split("&") if q and not q.startswith("authSource="))
    query = (query + "&" if query else "") + "authSource=admin"
    netloc = f"{quote_plus(username)}:{quote_plus(password)}@{host}"
    return urlunsplit((p.scheme, netloc, p.path or "/", query, ""))


@dataclass
class Identity:
    kind: str  # "generated" | "static" | "shared"
    uri: str | None = field(default=None, repr=False)
    username: str | None = None
    role: str | None = None
    expires_at: datetime | None = None

    def public(self) -> dict[str, Any]:
        """The agent_state.identity document: never the password or the URI."""
        return {"kind": self.kind, "username": self.username, "role": self.role,
                "expires_at": self.expires_at}


def _default_ping(uri: str) -> None:
    from pymongo import MongoClient

    c = MongoClient(uri, serverSelectionTimeoutMS=5000)
    try:
        c.admin.command("ping")
    finally:
        c.close()


class IdentityManager:
    """Provision / revoke / sync. One per process; ``admin`` and ``ping`` are injectable."""

    def __init__(self, creds: Credentials, admin: AtlasAdmin | None = None,
                 ping: Callable[[str], None] = _default_ping,
                 sleep: Callable[[float], None] = time.sleep):
        self.creds = creds
        self.admin = admin or AtlasAdmin(creds)
        self.ping = ping
        self.sleep = sleep
        self._roles: dict[tuple[str, str], dict] = {}
        self._lock = threading.Lock()

    def put_role(self, db: str, agent: str, perm: dict) -> str | None:
        """Create or update the role; None when it is already what we last sent."""
        body = role_payload(db, agent, perm)
        with self._lock:
            if self._roles.get((db, agent)) == body:
                return None
            result = self.admin.upsert_role(body)
            self._roles[(db, agent)] = body
        log.info("identity: role %s %s (%d actions)", body["roleName"], result,
                 len(body["actions"]))
        return result

    def provision(self, agent: str, db: str, perm: dict, base_uri: str) -> Identity:
        self.put_role(db, agent, perm)
        role = role_name(db, agent)
        username = user_prefix(db, agent) + secrets.token_hex(3)
        password = new_password()
        expires = (datetime.now(UTC) + timedelta(hours=ttl_hours())).replace(microsecond=0)
        self.admin.create_user({
            "databaseName": "admin",
            "username": username,
            "password": password,
            "roles": [{"roleName": role, "databaseName": "admin"}],
            "scopes": [{"name": self.creds.cluster, "type": "CLUSTER"}],
            "deleteAfterDate": expires.isoformat().replace("+00:00", "Z"),
            "description": f"Phylax Tower {agent} agent on {db} (generated)",
        })
        log.info("identity: user %s created for %s, expires %s", username, agent,
                 expires.isoformat())
        uri = user_uri(base_uri, username, password)
        deadline = time.monotonic() + PING_TIMEOUT_S
        while True:
            try:
                self.ping(uri)
                break
            except Exception as e:  # noqa: BLE001  (Atlas applies users asynchronously)
                if time.monotonic() >= deadline:
                    log.warning("identity: %s not usable after %.0fs (%s)", username,
                                PING_TIMEOUT_S, type(e).__name__)
                    raise
                self.sleep(3.0)
        return Identity("generated", uri, username, role, expires)

    def revoke(self, agent: str, db: str) -> list[str]:
        prefix = user_prefix(db, agent)
        gone = []
        for u in self.admin.list_users():
            name = u.get("username", "")
            if name.startswith(prefix):
                self.admin.delete_user(name, u.get("databaseName", "admin"))
                gone.append(name)
        try:
            self.admin.delete_role(role_name(db, agent))
        except AtlasError as e:
            log.warning("identity: role %s not deleted (%s)", role_name(db, agent), e)
        with self._lock:
            self._roles.pop((db, agent), None)
        log.info("identity: revoked %s on %s (%d users)", agent, db, len(gone))
        return gone

    def sync(self, database, agents: set[str] | None = None,
             stop: threading.Event | None = None) -> None:
        """Watch ``permissions`` and push read / write changes to the agents' roles."""
        stop = stop or threading.Event()
        for p in database.permissions.find():
            if agents is None or p["_id"] in agents:
                self._safe_put(database.name, p)
        with database.permissions.watch(full_document="updateLookup") as stream:
            while not stop.is_set():
                ch = stream.try_next()
                if ch is None:
                    stop.wait(1.0)
                    continue
                doc = ch.get("fullDocument")
                if doc and (agents is None or doc["_id"] in agents):
                    self._safe_put(database.name, doc)

    def _safe_put(self, db: str, perm: dict) -> None:
        if perm["_id"] == "system":
            return
        try:
            self.put_role(db, perm["_id"], perm)
        except Exception as e:  # noqa: BLE001
            log.warning("identity: role update for %s failed: %s", perm["_id"], e)


_manager: IdentityManager | None = None
_cache: dict[tuple[str, str], Identity] = {}
_cache_lock = threading.Lock()


def manager() -> IdentityManager | None:
    global _manager
    if _manager is None:
        creds = credentials()
        if creds is None:
            return None
        _manager = IdentityManager(creds)
    return _manager


def generated_identity(agent: str, db_name: str, shared_db) -> Identity | None:
    """Provision (once per process) and start the role sync thread; None without creds."""
    m = manager()
    if m is None:
        return None
    key = (db_name, agent)
    with _cache_lock:
        ident = _cache.get(key)
        if ident and ident.expires_at and ident.expires_at > datetime.now(UTC) + timedelta(
                minutes=10):
            return ident
        perm = shared_db.permissions.find_one({"_id": agent}) or {}
        ident = m.provision(agent, db_name, perm, os.environ["MONGODB_URI"])
        _cache[key] = ident
    threading.Thread(target=m.sync, args=(shared_db, {agent}), daemon=True,
                     name=f"identity-sync-{agent}").start()
    return ident
