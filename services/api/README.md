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
| `GET /v1/workspaces/{id}/datasets/{dataset_id}/contract` | Latest ingestion contract of a raw dataset (viewer+) |
| `GET …/contract/versions`, `GET …/contract/versions/{version}` | All contract versions, newest first, or one version (viewer+) |
| `POST …/contract` | Publish the next contract version (engineer+, raw datasets only); needs `based_on_version` |
| `POST …/contract/validate` | Check a sample file (raw request body, up to 5 MiB) against the latest or `?version=` contract (viewer+); nothing is stored |

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

## Ingestion contracts

A raw dataset's contract says what its source files must look like:

```json
{
  "format": "csv",
  "columns": [
    {"name": "order_id", "type": "string"},
    {"name": "amount", "type": "decimal"},
    {"name": "note", "type": "string", "nullable": true}
  ],
  "primary_key": ["order_id"],
  "based_on_version": 1
}
```

`format` is `csv` (header row required, column order free) or `jsonl` (one
JSON object per line). Column types are `string`, `integer`, `decimal`,
`date` (`YYYY-MM-DD`), `timestamp` (ISO 8601 with a UTC offset) and
`boolean`; in JSONL both native JSON values and their text forms are
accepted. Primary-key columns must exist and cannot be nullable.

Contracts are versioned and never edited in place: publishing adds
`latest + 1`, and `based_on_version` must equal the current latest
(`null` for the first) or the request fails with `409`. Older versions
stay readable so a file can be checked against the contract it arrived
under. Only `raw` datasets take contracts (`422` otherwise).

`POST …/contract/validate` reports every problem in a sample file:

| Rule | Meaning |
|---|---|
| `encoding` | Not valid UTF-8 |
| `empty_file` | No header (CSV) or no records (JSONL) |
| `duplicate_column` | A CSV header repeats a column |
| `missing_column`, `unexpected_column` | Header or record does not match the contract's columns |
| `malformed_record` | Wrong field count, unparseable CSV, or a line that is not a JSON object |
| `null_value` | Empty or `null` in a non-nullable column |
| `type_mismatch` | Value does not fit the column type |
| `duplicate_key` | Primary key repeats an earlier record |

```json
{"valid": false, "contract_version": 1, "record_count": 120, "violation_count": 1,
 "violations": [{"rule": "duplicate_key", "message": "order_id repeats record 17", "column": "order_id", "record": 54}],
 "truncated": false}
```

Records are numbered from 1, not counting the CSV header. At most 100
violations are listed (`truncated` is then `true`; `violation_count` is
the full total). Reports name columns and record numbers but never echo
source values. Larger samples answer `413`.

## Demo data

Seed a demo workspace (`acme-analytics`) with one synthetic user per role,
six datasets that mirror the synthetic sources in
[`packages/fixtures`](../../packages/fixtures) (`raw_*`, `stg_*`,
`daily_revenue`, owned by the engineer), a first ingestion contract for
each `raw_*` dataset that matches the fixture files, and print a fresh
token for each user:

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
`0004` adds `ingestion_contracts`; rolling it back drops every contract
version and keeps datasets.

## Tests

```bash
.venv/bin/pytest
```

Tests run against a throwaway SQLite file per test, so they need no
services. They cover liveness/readiness, request IDs, the error envelope,
upgrade → downgrade → upgrade, workspace, user and role constraints,
token authentication, the 401/404/403 role checks, member management,
the dataset registry (validation, workspace scoping, cursor pages,
optimistic-version conflicts, owner checks), ingestion contracts
(spec validation, versioning and `409` on stale publishes, every
validation rule and type, the violation cap, sample size limits), the
seed command, and that the ORM models match the migrations. Two tests
generate files with [`packages/fixtures`](../../packages/fixtures) and
check that clean files pass the seeded contracts and that the duplicate
order, null payment amount and renamed column defects are caught.
