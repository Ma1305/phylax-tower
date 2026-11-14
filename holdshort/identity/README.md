# holdshort/identity: per-agent Atlas identities

Each agent connects to Atlas as its **own database user**, whose privileges are generated from
its `permissions` document. "Who did this" (the Atlas user in the logs) and "what may it touch"
(the custom role) are then enforced by Atlas itself, on top of the validators and `ScopedDb`.

```
python -m holdshort.identity status     [--agent NAME] [--db NAME]   # what is set / missing
python -m holdshort.identity provision  [--agent NAME] [--db NAME]   # role + short-lived user
python -m holdshort.identity revoke     [--agent NAME] [--db NAME]   # delete users + role
python -m holdshort.identity sync       [--agent NAME] [--db NAME]   # follow permissions changes
```

## How it works

- `holdshort.access.connect_for(agent, process=..., db=...)`:
  1. Atlas admin credentials set -> provision once per process and connect as the generated user;
  2. else `MONGODB_URI_<AGENT>` (a static per-agent user you created yourself);
  3. else the shared `MONGODB_URI` (logged once: "credentials not set").
  The choice is written to `agent_state.<process>.identity =
  {kind: "generated"|"static"|"shared", username, role, expires_at}`. Never the password.
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
  their own. We ping with the new user for up to 90 s (Atlas applies changes asynchronously).
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
