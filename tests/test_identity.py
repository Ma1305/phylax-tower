"""holdshort.identity: role payloads, Atlas calls (mocked with httpx.MockTransport), fallback."""

import json
import logging
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

import httpx
import pytest

from holdshort import access
from holdshort.identity import atlas, core
from holdshort.identity.atlas import AtlasAdmin, Credentials
from holdshort.identity.core import INFRA, IdentityManager, role_payload

ATLAS_VARS = ("ATLAS_PROJECT_ID", "ATLAS_CLUSTER_NAME", "ATLAS_PUBLIC_KEY", "ATLAS_PRIVATE_KEY",
              "ATLAS_CLIENT_ID", "ATLAS_CLIENT_SECRET")
CREDS = Credentials("p" * 24, "Sandbox", public_key="pub", private_key="priv-secret")
SAFETY = {"_id": "safety", "read": ["context", "objects", "lane_status"],
          "write": ["intents_safety"]}
LISTENER = {"_id": "listener", "read": ["audio_clips", "streams"],
            "write": ["transmissions", "audio_clips"]}


def grants(body):
    out = {}
    for a in body["actions"]:
        for r in a["resources"]:
            assert r["db"] == "db_t" and r["cluster"] is False
            out.setdefault(r["collection"], set()).add(a["action"])
    return out


def test_role_payload_grants_exactly_the_listed_collections():
    body = role_payload("db_t", "safety", SAFETY, infra=False)
    assert body["roleName"] == "hs_db_t_safety"
    assert body["inheritedRoles"] == []
    assert grants(body) == {
        "context": {"FIND", "CHANGE_STREAM"},
        "objects": {"FIND", "CHANGE_STREAM"},
        "lane_status": {"FIND", "CHANGE_STREAM"},
        "intents_safety": {"FIND", "CHANGE_STREAM", "INSERT"},
    }


def test_listener_updates_only_audio_clips_and_infra_is_explicit():
    g = grants(role_payload("db_t", "listener", LISTENER))
    assert g["audio_clips"] == {"FIND", "CHANGE_STREAM", "INSERT", "UPDATE", "REMOVE"}
    assert g["transmissions"] == {"FIND", "CHANGE_STREAM", "INSERT"}
    assert g["streams"] == {"FIND", "CHANGE_STREAM"}
    extra = set(g) - {"audio_clips", "transmissions", "streams"}
    assert extra == set(INFRA)
    assert "decisions" not in g


class FakeAtlas:
    def __init__(self, role_exists=False):
        self.calls = []
        self.role_exists = role_exists
        self.users = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        self.calls.append((request.method, request.url.path, body))
        assert request.headers["accept"] == atlas.ACCEPT
        path = request.url.path
        if request.method == "POST" and path.endswith("/customDBRoles/roles"):
            if self.role_exists:
                return httpx.Response(409, json={"errorCode": "DUPLICATE"})
            self.role_exists = True
            return httpx.Response(202, json=body)
        if request.method == "PATCH":
            return httpx.Response(200, json=body)
        if request.method == "POST" and path.endswith("/databaseUsers"):
            self.users.append(body)
            return httpx.Response(201, json={k: v for k, v in body.items() if k != "password"})
        if request.method == "GET" and path.endswith("/databaseUsers"):
            return httpx.Response(200, json={"results": self.users})
        if request.method == "DELETE":
            return httpx.Response(204)
        return httpx.Response(404, json={})


def make_manager(fake, pings=None):
    attempts = []

    def ping(uri):
        attempts.append(uri)
        if pings and len(attempts) <= pings:
            raise RuntimeError("auth failed")

    m = IdentityManager(CREDS, admin=AtlasAdmin(CREDS, transport=httpx.MockTransport(fake)),
                        ping=ping, sleep=lambda s: None)
    return m, attempts


def test_write_list_change_produces_role_update():
    fake = FakeAtlas()
    m, _ = make_manager(fake)
    assert m.put_role("db_t", "safety", SAFETY) == "created"
    assert m.put_role("db_t", "safety", SAFETY) is None  # unchanged -> no call
    changed = {**SAFETY, "write": ["intents_safety", "decisions"]}
    assert m.put_role("db_t", "safety", changed) == "updated"
    method, path, body = fake.calls[-1]
    assert method == "PATCH" and path.endswith("/customDBRoles/roles/hs_db_t_safety")
    assert "INSERT" in grants({"actions": body["actions"]})["decisions"]


def test_provision_sets_expiry_scope_and_hides_password(caplog, monkeypatch):
    monkeypatch.setenv("AGENT_USER_TTL_HOURS", "6")
    caplog.set_level(logging.DEBUG)
    fake = FakeAtlas()
    m, attempts = make_manager(fake, pings=2)
    ident = m.provision("safety", "db_t", SAFETY,
                        "mongodb+srv://admin:adminpw@cluster0.x.mongodb.net/?retryWrites=true")
    user = fake.users[0]
    pw = user["password"]
    assert len(pw) == 32 and pw.isalnum()
    assert user["username"].startswith("hs-db_t-safety-")
    assert user["roles"] == [{"roleName": "hs_db_t_safety", "databaseName": "admin"}]
    assert user["scopes"] == [{"name": "Sandbox", "type": "CLUSTER"}]
    expiry = datetime.fromisoformat(user["deleteAfterDate"].replace("Z", "+00:00"))
    assert timedelta(hours=5.9) < expiry - datetime.now(UTC) < timedelta(hours=6.1)
    assert ident.expires_at == expiry
    assert len(attempts) == 3  # retried until the user could authenticate
    assert "cluster0.x.mongodb.net" in ident.uri and "adminpw" not in ident.uri
    assert pw in ident.uri
    assert pw not in caplog.text and pw not in json.dumps(ident.public(), default=str)
    assert pw not in repr(ident)


def test_revoke_deletes_users_and_role():
    fake = FakeAtlas()
    fake.users = [{"username": "hs-db_t-safety-abc123", "databaseName": "admin"},
                  {"username": "someone-else", "databaseName": "admin"}]
    m, _ = make_manager(fake)
    assert m.revoke("safety", "db_t") == ["hs-db_t-safety-abc123"]
    deletes = [p for meth, p, _ in fake.calls if meth == "DELETE"]
    assert any(p.endswith("/databaseUsers/admin/hs-db_t-safety-abc123") for p in deletes)
    assert any(p.endswith("/customDBRoles/roles/hs_db_t_safety") for p in deletes)


def test_service_account_uses_bearer_token():
    seen = []

    def handler(request):
        seen.append(request)
        if request.url.path == "/api/oauth/token":
            return httpx.Response(200, json={"access_token": "tok", "expires_in": 3600})
        return httpx.Response(200, json={"results": []})

    creds = Credentials("p" * 24, "Sandbox", client_id="cid", client_secret="csecret")
    AtlasAdmin(creds, transport=httpx.MockTransport(handler)).list_users()
    assert seen[0].url.path == "/api/oauth/token"
    assert seen[1].headers["authorization"] == "Bearer tok"


@pytest.fixture
def no_atlas(monkeypatch):
    for v in ATLAS_VARS + ("MONGODB_URI_SAFETY", "AGENT_IDENTITIES"):
        monkeypatch.delenv(v, raising=False)
    monkeypatch.setattr(core, "_manager", None)
    monkeypatch.setattr(access, "_identity_logged", False)
    shared = MagicMock(name="shared_client")
    monkeypatch.setattr(access, "get_client", lambda uri=None, **kw: shared if uri is None
                        else MagicMock(name=f"client:{uri}"))
    return shared


def test_no_credentials_shared_connection_and_one_log_line(no_atlas, caplog):
    caplog.set_level(logging.INFO)
    c1 = access.connect_for("safety", process="safety_agent", db="db_t")
    c2 = access.connect_for("safety", process="safety_agent", db="db_t")
    assert c1 is no_atlas and c2 is no_atlas
    lines = [r for r in caplog.records if "generated identities off" in r.getMessage()]
    assert len(lines) == 1
    call = no_atlas["db_t"].agent_state.update_one.call_args
    assert call.args[0] == {"_id": "safety_agent"}
    assert call.args[1]["$set"]["identity"] == {"kind": "shared", "username": None,
                                                "role": None, "expires_at": None}


def test_static_uri_when_set(no_atlas, monkeypatch):
    monkeypatch.setenv("MONGODB_URI_SAFETY", "mongodb+srv://s:pw@h/")
    c = access.connect_for("safety", db="db_t")
    assert c is not no_atlas
    ident = no_atlas["db_t"].agent_state.update_one.call_args.args[1]["$set"]["identity"]
    assert ident["kind"] == "static"
    assert "pw" not in json.dumps(ident)


def _atlas_env(monkeypatch, switch="generated"):
    for v, val in zip(ATLAS_VARS[:4], ("p" * 24, "Sandbox", "pub", "priv-secret"), strict=True):
        monkeypatch.setenv(v, val)
    if switch:
        monkeypatch.setenv("AGENT_IDENTITIES", switch)


def test_credentials_without_switch_never_call_atlas(no_atlas, monkeypatch):
    _atlas_env(monkeypatch, switch=None)
    called = []
    monkeypatch.setattr(core, "IdentityManager", lambda *a, **k: called.append(1))
    assert access.connect_for("safety", db="db_t") is no_atlas
    assert core.manager() is None and not called
    ident = no_atlas["db_t"].agent_state.update_one.call_args.args[1]["$set"]["identity"]
    assert ident["kind"] == "shared"


def test_not_ready_in_time_stays_shared_and_drops_user(no_atlas, monkeypatch):
    _atlas_env(monkeypatch)
    monkeypatch.setenv("MONGODB_URI", "mongodb+srv://admin:adminpw@h.mongodb.net/")
    monkeypatch.setenv("AGENT_IDENTITY_WAIT_S", "0")
    fake = FakeAtlas()
    m, _ = make_manager(fake, pings=99)
    monkeypatch.setattr(core, "_manager", m)
    monkeypatch.setattr(core, "_cache", {})
    dropped = []
    monkeypatch.setattr(core, "_drop_user", lambda mgr, ident: dropped.append(ident.username))
    monkeypatch.setattr(core.threading, "Thread",
                        lambda target, args, **k: MagicMock(start=lambda: target(*args)))
    no_atlas["db_t"].permissions.find_one.return_value = SAFETY
    assert access.connect_for("safety", process="safety_agent", db="db_t") is no_atlas
    ident = no_atlas["db_t"].agent_state.update_one.call_args.args[1]["$set"]["identity"]
    assert ident["kind"] == "shared" and "not usable in time" in ident["note"]
    assert dropped == [fake.users[0]["username"]]


def test_provision_is_idempotent_one_user_per_agent():
    fake = FakeAtlas()
    fake.users = [{"username": "hs-db_t-safety-old111", "databaseName": "admin"},
                  {"username": "keep-me", "databaseName": "admin"}]
    m, _ = make_manager(fake)
    m.provision("safety", "db_t", SAFETY, "mongodb+srv://a:b@h/")
    deletes = [p for meth, p, _ in fake.calls if meth == "DELETE"]
    assert deletes == [deletes[0]] and deletes[0].endswith("/admin/hs-db_t-safety-old111")


def test_generated_identity_recorded_without_password(no_atlas, monkeypatch, caplog):
    _atlas_env(monkeypatch)
    monkeypatch.setenv("MONGODB_URI", "mongodb+srv://admin:adminpw@h.mongodb.net/")
    fake = FakeAtlas()
    m, _ = make_manager(fake)
    m.sync = lambda *a, **k: None
    monkeypatch.setattr(core, "_manager", m)
    monkeypatch.setattr(core, "_cache", {})
    no_atlas["db_t"].permissions.find_one.return_value = SAFETY
    caplog.set_level(logging.DEBUG)
    access.connect_for("safety", process="safety_agent", db="db_t")
    pw = fake.users[0]["password"]
    ident = no_atlas["db_t"].agent_state.update_one.call_args.args[1]["$set"]["identity"]
    assert ident["kind"] == "generated" and ident["role"] == "hs_db_t_safety"
    assert ident["username"] == fake.users[0]["username"] and ident["expires_at"]
    assert pw not in json.dumps(ident, default=str) and pw not in caplog.text
