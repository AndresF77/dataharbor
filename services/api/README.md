# DataHarbor API

FastAPI service that owns DataHarbor's metadata: workspaces and the
dataset registry now, and ingestion contracts and run history as they are
added.
Schema changes are Alembic migrations in [`migrations/versions`](migrations/versions).

## Run locally

```bash
cd services/api
python3 -m venv .venv
.venv/bin/pip install -e '.[test]'

# PostgreSQL from infra/compose (see ../../infra/compose/README.md)
export DATAHARBOR_DATABASE_URL=postgresql+psycopg://dataharbor:<POSTGRES_PASSWORD>@127.0.0.1:5432/dataharbor
.venv/bin/alembic upgrade head
.venv/bin/uvicorn dataharbor_api.main:create_app --factory --reload
```

| Variable | Default | Meaning |
|---|---|---|
| `DATAHARBOR_DATABASE_URL` | local Compose PostgreSQL | SQLAlchemy URL of the metadata database |
| `DATAHARBOR_ENVIRONMENT` | `local` | `local`, `test` or `ci` |
| `DATAHARBOR_LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING` or `ERROR` |

## Endpoints

| Route | Purpose |
|---|---|
| `GET /health` | Liveness; never touches the database |
| `GET /health/ready` | `200` when the database answers and the schema is at the latest migration, otherwise `503` with the failing check |
| `GET /v1/openapi.json`, `GET /v1/docs` | OpenAPI contract and interactive docs |
| `GET /v1/workspaces` | Workspaces the caller belongs to, with their role |
| `POST /v1/workspaces` | Create a workspace; the caller becomes its first admin (`409` on a taken slug) |
| `GET /v1/workspaces/{id}` | Workspace detail (viewer+) |
| `GET /v1/workspaces/{id}/members` | Members and roles (viewer+) |
| `PUT /v1/workspaces/{id}/members/{user_id}` | Add a member or change their role (admin) |
| `DELETE /v1/workspaces/{id}/members/{user_id}` | Remove a member (admin); clears their dataset ownership |
| `GET /v1/workspaces/{id}/datasets` | Registered datasets by name (viewer+); `layer` filter, `limit` 1–200, `cursor` |
| `POST /v1/workspaces/{id}/datasets` | Register a dataset (engineer+); `409` on a taken name |
| `GET /v1/workspaces/{id}/datasets/{dataset_id}` | Dataset detail (viewer+) |
| `PATCH /v1/workspaces/{id}/datasets/{dataset_id}` | Edit layer, description, owner or freshness SLA (engineer+); needs the current `version` |

## Authentication and roles

`/v1/workspaces` routes need `Authorization: Bearer <token>`. Only the
SHA-256 of each token is stored; revoked tokens are rejected. Roles are
`viewer` < `engineer` < `admin`, and each role includes the ones below it.
Membership is checked server-side on every request:

| Situation | Response |
|---|---|
| No, unknown or revoked token | `401` with `WWW-Authenticate: Bearer` |
| Not a member of the workspace | `404` (the workspace's existence is not revealed) |
| Member without the required role | `403` |
| Demoting or removing the last admin | `409` |

## Dataset registry

Each dataset has a name unique within its workspace (`^[a-z][a-z0-9_]{1,62}$`,
e.g. `stg_orders`), a layer (`raw`, `staging` or `mart`), a description,
an optional owner who must be a workspace member (`422` otherwise) and an
optional freshness SLA in minutes (1 to 525,600). Names cannot be changed.

Every edit must send the `version` it is based on; the server bumps it on
success and answers `409` if the dataset changed in the meantime, so
concurrent edits never overwrite each other silently. In `PATCH`, omitted
fields stay as they are and `null` clears the owner or SLA.

Lists are ordered by name and paged with an opaque cursor:

```json
{"items": [{"id": "…", "name": "raw_orders", "layer": "raw", "version": 1, "…": "…"}], "next_cursor": "cmF3X29yZGVycw"}
```

Pass `next_cursor` back as `?cursor=` until it is `null`. A dataset in
another workspace answers `404`, the same as an unknown ID.

## Demo data

Seed a demo workspace (`acme-analytics`) with one synthetic user per role,
six datasets that mirror the synthetic sources in
[`packages/fixtures`](../../packages/fixtures) (`raw_*`, `stg_*`,
`daily_revenue`, owned by the engineer), and print a fresh token for each
user:

```bash
.venv/bin/python -m dataharbor_api.seed
```

Every response carries an `X-Request-ID` header: a safe inbound value
(8–64 characters of `A-Z a-z 0-9 . _ -`) is echoed, anything else is
replaced. Errors share one envelope:

```json
{"error": {"code": "not_found", "message": "Not Found", "request_id": "…", "details": null}}
```

Validation errors (`422`) list the failing fields but never echo the
rejected input.

## Migrations

```bash
.venv/bin/alembic upgrade head          # apply
.venv/bin/alembic downgrade -1          # roll back one step
.venv/bin/alembic upgrade head --sql    # print the PostgreSQL DDL without connecting
```

Each migration's docstring states its rollback behaviour. `0001` creates
`workspaces`, the tenant root that later tables reference. `0002` adds
`users`, `api_tokens` and `workspace_members`; rolling it back drops those
three tables and keeps workspaces. `0003` adds `datasets`; rolling it back
drops every registered dataset and keeps workspaces, users and members.

## Tests

```bash
.venv/bin/pytest
```

Tests run against a throwaway SQLite file per test, so they need no
services. They cover liveness/readiness, request IDs, the error envelope,
upgrade → downgrade → upgrade, workspace, user and role constraints,
token authentication, the 401/404/403 role checks, member management,
the dataset registry (validation, workspace scoping, cursor pages,
optimistic-version conflicts, owner checks), the seed command, and that
the ORM models match the migrations.
