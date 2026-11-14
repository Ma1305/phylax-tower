"""A tiny Atlas Administration API v2 client (httpx). Digest auth or a service account.

Endpoints, confirmed against https://www.mongodb.com/docs/api/doc/atlas-admin-api-v2/ :
  POST   /groups/{groupId}/customDBRoles/roles
  PATCH  /groups/{groupId}/customDBRoles/roles/{roleName}
  DELETE /groups/{groupId}/customDBRoles/roles/{roleName}
  GET    /groups/{groupId}/databaseUsers
  POST   /groups/{groupId}/databaseUsers
  DELETE /groups/{groupId}/databaseUsers/{databaseName}/{username}
Service-account tokens: POST https://cloud.mongodb.com/api/oauth/token (client credentials).
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import Any

import httpx

BASE = "https://cloud.mongodb.com/api/atlas/v2"
TOKEN_URL = "https://cloud.mongodb.com/api/oauth/token"
ACCEPT = "application/vnd.atlas.2023-01-01+json"


class AtlasError(RuntimeError):
    def __init__(self, status: int, detail: str):
        super().__init__(f"Atlas API {status}: {detail}")
        self.status = status


@dataclass(frozen=True)
class Credentials:
    project_id: str
    cluster: str
    public_key: str = ""
    private_key: str = ""
    client_id: str = ""
    client_secret: str = ""

    @property
    def kind(self) -> str:
        return "service_account" if self.client_id else "api_key"


def _env(name: str) -> str:
    return os.environ.get(name, "").strip()


def mode() -> str:
    """AGENT_IDENTITIES: "generated" turns per-agent users on; anything else (default) is off."""
    return _env("AGENT_IDENTITIES").lower() or "off"


def enabled() -> bool:
    """Generated identities are used only when AGENT_IDENTITIES=generated, never implicitly."""
    return mode() == "generated"


def missing() -> list[str]:
    """Names of the environment variables still needed for generated identities."""
    out = [n for n in ("ATLAS_PROJECT_ID", "ATLAS_CLUSTER_NAME") if not _env(n)]
    key = _env("ATLAS_PUBLIC_KEY") and _env("ATLAS_PRIVATE_KEY")
    sa = _env("ATLAS_CLIENT_ID") and _env("ATLAS_CLIENT_SECRET")
    if not (key or sa):
        out.append("ATLAS_PUBLIC_KEY+ATLAS_PRIVATE_KEY or ATLAS_CLIENT_ID+ATLAS_CLIENT_SECRET")
    return out


def credentials() -> Credentials | None:
    if missing():
        return None
    if _env("ATLAS_CLIENT_ID") and _env("ATLAS_CLIENT_SECRET"):
        return Credentials(_env("ATLAS_PROJECT_ID"), _env("ATLAS_CLUSTER_NAME"),
                           client_id=_env("ATLAS_CLIENT_ID"),
                           client_secret=_env("ATLAS_CLIENT_SECRET"))
    return Credentials(_env("ATLAS_PROJECT_ID"), _env("ATLAS_CLUSTER_NAME"),
                       public_key=_env("ATLAS_PUBLIC_KEY"), private_key=_env("ATLAS_PRIVATE_KEY"))


class AtlasAdmin:
    """Calls the few endpoints we need. ``transport`` lets tests mock HTTP."""

    def __init__(self, creds: Credentials, transport: httpx.BaseTransport | None = None):
        self.creds = creds
        self._token: tuple[str, float] | None = None
        auth = (httpx.DigestAuth(creds.public_key, creds.private_key)
                if creds.kind == "api_key" else None)
        self._http = httpx.Client(transport=transport, auth=auth, timeout=30.0,
                                  headers={"Accept": ACCEPT, "Content-Type": ACCEPT})

    def _bearer(self) -> dict[str, str]:
        if self.creds.kind != "service_account":
            return {}
        if not self._token or self._token[1] < time.time() + 60:
            r = self._http.post(TOKEN_URL, data={"grant_type": "client_credentials"},
                                auth=(self.creds.client_id, self.creds.client_secret),
                                headers={"Accept": "application/json",
                                         "Content-Type": "application/x-www-form-urlencoded"})
            if r.status_code >= 400:
                raise AtlasError(r.status_code, "service account token request failed")
            body = r.json()
            self._token = (body["access_token"], time.time() + float(body.get("expires_in", 3600)))
        return {"Authorization": f"Bearer {self._token[0]}"}

    def request(self, method: str, path: str, json: Any = None) -> Any:
        url = f"{BASE}/groups/{self.creds.project_id}{path}"
        r = self._http.request(method, url, json=json, headers=self._bearer())
        if r.status_code >= 400:
            try:
                b = r.json()
                detail = f"{b.get('errorCode')}: {b.get('detail') or b.get('reason')}"
            except ValueError:
                detail = r.reason_phrase
            raise AtlasError(r.status_code, detail)
        return r.json() if r.content else None

    # custom roles
    def upsert_role(self, role: dict) -> str:
        """Create the role, or PATCH its actions if it exists. Returns "created"|"updated"."""
        try:
            self.request("POST", "/customDBRoles/roles", role)
            return "created"
        except AtlasError as e:
            if e.status != 409:
                raise
        body = {k: v for k, v in role.items() if k != "roleName"}
        self.request("PATCH", f"/customDBRoles/roles/{role['roleName']}", body)
        return "updated"

    def delete_role(self, name: str) -> None:
        try:
            self.request("DELETE", f"/customDBRoles/roles/{name}")
        except AtlasError as e:
            if e.status != 404:
                raise

    # database users
    def create_user(self, user: dict) -> None:
        self.request("POST", "/databaseUsers", user)

    def list_users(self) -> list[dict]:
        out: list[dict] = []
        page = 1
        while True:
            body = self.request("GET", f"/databaseUsers?itemsPerPage=500&pageNum={page}") or {}
            results = body.get("results", [])
            out.extend(results)
            if len(results) < 500:
                return out
            page += 1

    def list_roles(self) -> list[dict]:
        return self.request("GET", "/customDBRoles/roles") or []

    def get_role(self, name: str) -> dict | None:
        try:
            return self.request("GET", f"/customDBRoles/roles/{name}")
        except AtlasError as e:
            if e.status == 404:
                return None
            raise

    def delete_user(self, username: str, database: str = "admin") -> None:
        try:
            self.request("DELETE", f"/databaseUsers/{database}/{username}")
        except AtlasError as e:
            if e.status != 404:
                raise

    def close(self) -> None:
        self._http.close()
