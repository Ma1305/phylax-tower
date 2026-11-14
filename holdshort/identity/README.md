# holdshort/identity: per-agent Atlas identities

Each agent connects to Atlas as its **own database user**, whose privileges are generated from
its `permissions` document. "Who did this" (the Atlas user in the logs) and "what may it touch"
(the custom role) are then enforced by Atlas itself, on top of the validators and `ScopedDb`.

**Off by default.** Generated identities are used only when `AGENT_IDENTITIES=generated`,
even when every `ATLAS_*` variable is present. Tests, the end-to-end check and other tasks
never create Atlas users implicitly.

```
python -m holdshort.identity status     [--agent NAME] [--db NAME]          # switch + config
python -m holdshort.identity provision  (--agent NAME | --all) [--db NAME]  # role + user
python -m holdshort.identity revoke     (--agent NAME | --all) [--db NAME]  # users + role
python -m holdshort.identity sync       [--agent NAME] [--db NAME]          # follow permissions
python -m holdshort.identity.live_check --db holdshort_v2_t<N>_id           # real-Atlas proof
```
`provision` and `sync` refuse to run unless the switch is on; `revoke` always works.

## Measured on the real Atlas project (2026-11-14, cluster on MongoDB 9.0.7)

| What | Time |
|---|---|
| Admin API calls (role POST/PATCH + user POST) | 0.3 s |
| New user can authenticate, Atlas idle | **3.1 s** |
| New user can authenticate, other changes queued (old one-by-one `provision --all`) | **65-91 s** each (~5 min for 4) |
| `provision --all` now (create all, wait together): all 4 users usable | **58.7 s** (64 s total) |
| Role PATCHed after a `permissions` read-list change (sync, change stream) | 1.5 s |
| ...and the user's new privilege enforced by the server | **27 s** |

`live_check` proved, as the generated safety user over a raw `MongoClient` (no ScopedDb):
reads of `lane_status` / `context` and an insert into `intents_safety` succeed; a read of
`transmissions`, an insert into `decisions` and an insert into `intents_procedures` are
refused by the server with **code 13 Unauthorized**. No payload, action name or scope was
rejected by the real API; the only non-2xx answers were the digest-auth 401 challenge and the
expected 409 when a role already exists (then PATCH). Atlas applies changes as a project-wide
queue, so many changes at once are slow: `provision --all` creates everything first and then
waits for all users together (up to `PING_TIMEOUT_S` = 180 s).

## Turn it on for a running stack

1. In `.env`: `ATLAS_PROJECT_ID`, `ATLAS_CLUSTER_NAME`, the key pair (see Credentials) and
   `AGENT_IDENTITIES=generated`. Optionally `AGENT_IDENTITY_WAIT_S=120` (see below).
2. `uv run python -m holdshort.identity status --db holdshort_v2` -> `generated identities: ON`
   and the actions each role would get.
3. Restart the agent processes (listener, safety, procedures, weather). Each one provisions
   its own role + user at startup (`connect_for`); nothing else has to run.
4. Check `agent_state.<process>.identity.kind` is `generated` (with `username`, `role`,
   `expires_at`, `ready_s`). `shared` + a `note` means it fell back (next section).
5. Turn off: set `AGENT_IDENTITIES=off` (or remove it), restart the agents, then
   `uv run python -m holdshort.identity revoke --all --db holdshort_v2`.

### Startup never blocks

An agent waits at most `AGENT_IDENTITY_WAIT_S` (default 20 s) for its new user. If the user
is not usable by then, the agent **stays on the shared connection (or `MONGODB_URI_<AGENT>`)
for the life of that process**, records `identity = {kind: "shared", note: "generated user
... not usable in time; shared until restart"}`, and deletes the unusable user in the
background so no orphan is left. Restart it later to try again. Because Atlas can take over a
minute when several agents start together, set `AGENT_IDENTITY_WAIT_S=120` when you want the
generated users in a demo. Provisioning is idempotent: each provision upserts the role and
replaces earlier `hs-<db>-<agent>-*` users, so there is one generated user per agent per
database.

## How it works

- `holdshort.access.connect_for(agent, process=..., db=...)`:
  1. `AGENT_IDENTITIES=generated` and Atlas admin credentials set -> provision once per process and connect as the generated user;
  2. else `MONGODB_URI_<AGENT>` (a static per-agent user you created yourself);
  3. else the shared `MONGODB_URI` (logged once: "generated identities off (...)").
  The choice is written to `agent_state.<process>.identity =
  {kind: "generated"|"static"|"shared", username, role, expires_at, ready_s?, note?}`.
  Never the password.
  Any identity error falls back to 2/3 with a warning; it never stops the process.
- **Role** `hs_<db>_<agent>` (custom DB role, defined on `admin`, resources on `<db>` only):
  `FIND` + `CHANGE_STREAM` on every collection in `read` or `write`, `INSERT` on each `write`
  collection, `UPDATE` + `REMOVE` only where the contract says the agent updates
  (`audio_clips` for the listener).
  **Infrastructure grants** (`core.INFRA`, needed for any worker to run and for the audit
  trail itself): `agent_state` find/insert/update (resume tokens, identity),
  `security_events` insert (ScopedDb records denied calls), `permissions` find (ScopedDb reloads
  the agent's own lists). Nothing else.
- **User** `hs-<db>-<agent>-<6 hex>`, auth db `admin`, roles = that role only, `scopes` = the
  cluster only, random 32-character alphanumeric password, `deleteAfterDate` = now +
  `AGENT_USER_TTL_HOURS` (default 6; Atlas allows at most one week) so stale users disappear on
  their own. We ping with the new user (Atlas applies changes asynchronously): up to
  `AGENT_IDENTITY_WAIT_S` at agent startup, up to 180 s in the `provision` command.
- **Sync**: a daemon thread per provisioned agent watches `permissions` (change stream,
  `updateLookup`) and PATCHes the role when the read / write lists change, so a controller's
  change is enforced by Atlas within about a minute, without restarting the agent.

## Credentials (.env, all optional)

`ATLAS_PROJECT_ID`, `ATLAS_CLUSTER_NAME`, and either
- `ATLAS_PUBLIC_KEY` + `ATLAS_PRIVATE_KEY` (programmatic API key, HTTP digest), or
- `ATLAS_CLIENT_ID` + `ATLAS_CLIENT_SECRET` (service account; OAuth client credentials at
  `https://cloud.mongodb.com/api/oauth/token`, bearer token).

**Atlas role needed: `Project Database Access Admin`** on the project (the documented role
requirement for creating / updating / deleting custom roles and database users; listing users
needs `Project Read Only`, which it includes). Do not grant Project Owner.

Create it: Atlas UI -> your project -> *Project Identity & Access* (Access Manager) ->
*Applications* -> *Service Accounts* (or *API Keys*) -> *Create* -> project permission
**Project Database Access Admin** -> copy the client id / secret (or public / private key) into
`.env`. Add the machine's IP to the key's API access list if the organization requires one.

## API used (Atlas Administration API v2, `Accept: application/vnd.atlas.2023-01-01+json`)

| Call | Doc |
|---|---|
| `POST /api/atlas/v2/groups/{groupId}/customDBRoles/roles` | https://www.mongodb.com/docs/api/doc/atlas-admin-api-v2/operation/operation-creategroupcustomdbrolerole |
| `PATCH .../customDBRoles/roles/{roleName}` | https://www.mongodb.com/docs/api/doc/atlas-admin-api-v2/operation/operation-updategroupcustomdbrolerole |
| `DELETE .../customDBRoles/roles/{roleName}` | https://www.mongodb.com/docs/api/doc/atlas-admin-api-v2/operation/operation-deletegroupcustomdbrolerole |
| `POST /api/atlas/v2/groups/{groupId}/databaseUsers` | https://www.mongodb.com/docs/api/doc/atlas-admin-api-v2/operation/operation-creategroupdatabaseuser |
| `GET .../customDBRoles/roles[/{roleName}]` | https://www.mongodb.com/docs/api/doc/atlas-admin-api-v2/operation/operation-listgroupcustomdbroleroles |
| `GET .../databaseUsers` | https://www.mongodb.com/docs/api/doc/atlas-admin-api-v2/operation/operation-listgroupdatabaseusers |
| `DELETE .../databaseUsers/{databaseName}/{username}` | https://www.mongodb.com/docs/api/doc/atlas-admin-api-v2/operation/operation-deletegroupdatabaseuser |

Payload shapes and the action enum (`FIND`, `INSERT`, `UPDATE`, `REMOVE`, `CHANGE_STREAM`, ...;
resources `{cluster, db, collection}`; user `roles[{roleName, databaseName}]`,
`scopes[{name, type: "CLUSTER"}]`, `deleteAfterDate`) were checked against the OpenAPI spec
https://www.mongodb.com/docs/api/doc/atlas-admin-api-v2.json (schemas
`UserCustomDBRole`, `DatabasePrivilegeAction`, `DatabasePermittedNamespaceResource`,
`CloudDatabaseUser`, `DatabaseUserRole`, `UserScope`). Service accounts:
https://www.mongodb.com/docs/atlas/api/service-accounts-overview/ .
Custom roles in Atlas: https://www.mongodb.com/docs/atlas/security-add-mongodb-roles/ .

Tests: `tests/test_identity.py` (HTTP mocked with `httpx.MockTransport`; no Atlas admin calls).
