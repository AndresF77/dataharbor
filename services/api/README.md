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
| `DATAHARBOR_OBJECT_STORE` | `minio` | `minio`, or `memory` to keep landed files in the process (no MinIO needed; lost on restart) |
| `DATAHARBOR_MINIO_ENDPOINT` | `http://127.0.0.1:9000` | MinIO / S3 API URL |
| `DATAHARBOR_MINIO_ACCESS_KEY`, `DATAHARBOR_MINIO_SECRET_KEY` | local Compose MinIO | MinIO credentials (`MINIO_ROOT_USER` / `MINIO_ROOT_PASSWORD`) |
| `DATAHARBOR_LANDING_BUCKET` | `dataharbor-landing-local` | Bucket created by `minio-init` |
| `DATAHARBOR_MAX_LANDING_BYTES` | `52428800` (50 MiB) | Largest file accepted by the landing endpoint |

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
| `POST /v1/workspaces/{id}/datasets/{dataset_id}/files` | Land a source file (raw request body, `business_date`, `filename`; engineer+) in MinIO after it passes the latest contract |
| `GET …/files`, `GET …/files/{file_id}` | Landed files, newest business date first, or one file (viewer+) |
| `POST …/files/{file_id}/verify` | Re-read the stored object and compare its SHA-256 (engineer+) |
| `GET /v1/workspaces/{id}/pipelines`, `GET …/pipelines/{pipeline_id}` | Registered pipelines (viewer+) |
| `POST …/pipelines/{pipeline_id}/runs` | Open a run for a business date (engineer+); same `external_run_id` again → `200` with the same run |
| `GET …/runs/{run_id}` | One run with its task outcomes (viewer+) |
| `POST …/runs/{run_id}/tasks` | Record a task outcome once (engineer+) |
| `POST …/runs/{run_id}/finish` | Close the run as `succeeded` or `failed` (engineer+) |
| `GET /v1/workspaces/{id}/datasets/{dataset_id}/freshness-rule` | The dataset's freshness rule (viewer+) |
| `PUT …/freshness-rule` | Create (no `version`) or replace (current `version`) the rule (engineer+) |
| `DELETE …/freshness-rule` | Remove the rule (engineer+) |
| `GET /v1/workspaces/{id}/datasets/{dataset_id}/freshness` | Evaluate the rule now: `fresh`, `warning`, `stale` or `unknown` (viewer+) |
| `GET /v1/workspaces/{id}/freshness` | Every dataset with a rule, worst status first, with counts; repeatable `status` filter (viewer+) |
| `GET /v1/workspaces/{id}/datasets/{dataset_id}/quality-rules`, `GET …/quality-rules/{rule_id}` | Data quality rules of a dataset, by name (viewer+) |
| `POST …/quality-rules` | Add a rule (engineer+, raw datasets with a contract); checked against the latest contract |
| `PUT …/quality-rules/{rule_id}`, `DELETE …/quality-rules/{rule_id}` | Replace (current `version`) or remove a rule (engineer+); past results stay |
| `POST …/files/{file_id}/quality-checks` | Run the enabled rules on a landed file and record the results (engineer+) |
| `GET …/datasets/{dataset_id}/quality-results`, `GET /v1/workspaces/{id}/quality-results` | Result history, newest first, with filters (viewer+) |

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

## Landing zone

Engineers land a raw dataset's source file by sending it as the request body:

```bash
curl -X POST \
  "http://127.0.0.1:8000/v1/workspaces/$WS/datasets/$DS/files?business_date=2026-03-01&filename=orders_2026-03-01.csv" \
  -H "Authorization: Bearer $TOKEN" \
  -H "X-Content-SHA256: $(sha256sum orders_2026-03-01.csv | cut -d' ' -f1)" \
  --data-binary @orders_2026-03-01.csv
```

The file is checked against the dataset's latest ingestion contract. A
file that fails answers `422` with the same violation report as
`…/contract/validate` in `error.details`, and nothing is stored. A passing
file is written to the landing bucket under a content-addressed key

```
workspaces/<workspace id>/<dataset name>/business_date=<YYYY-MM-DD>/<sha256>.<csv|jsonl>
```

and recorded in `landed_files` (contract version, business date, original
file name, SHA-256, size, record count, who landed it). The answer is `201`
with `"duplicate": false`; sending the same bytes again answers `200` with
the existing record and `"duplicate": true`, so retries never store a
second copy.

| Check | Answer |
|---|---|
| `X-Content-SHA256` (optional) differs from the body | `422`, nothing stored |
| Dataset is not `raw`, or has no contract yet | `422` |
| Empty body / larger than `DATAHARBOR_MAX_LANDING_BYTES` | `422` / `413` |
| `filename` not `A-Z a-z 0-9 . _ -` (no paths) or bad `business_date` | `422` |
| MinIO unreachable | `503`, nothing recorded; retry later |

The SHA-256 is also sent to MinIO as `x-amz-checksum-sha256`, so the store
rejects bytes that change in transit, and kept as object metadata.
`GET …/files` lists landed files (newest business date first, optional
`business_date` filter, `limit` 1–200), `GET …/files/{id}` shows one, and
`POST …/files/{id}/verify` (engineer) re-reads the object and reports
whether its SHA-256 still matches. Logs record ids, sizes and checksums,
never file contents.

## Pipeline runs

A pipeline is an orchestrated job: the Airflow DAG id, its schedule and its
kind (`ingestion` or `transformation`). The DAGs in
[`services/worker`](../worker/README.md) report every run here, so each
execution has a stable DataHarbor run ID and a history that does not change
once the run is finished ([ADR 0002](../../docs/adr/0002-airflow-run-tracking.md)).

| Step | Rule |
|---|---|
| Start (`external_run_id`, `business_date`, `trigger` = `scheduled` / `manual` / `backfill`) | `external_run_id` (Airflow `run_id`) is unique per pipeline: a retried start returns the same run (`200`); the same id with another date or trigger is `409` |
| Task outcome (`task_id`, `status` = `succeeded` / `failed` / `skipped`, optional `dataset_id`, `landed_file_id`, `message` ≤ 500 chars) | Written once: the identical report again is `200`, a different one `409`. `dataset_id` must be in the workspace and `landed_file_id` a file of that dataset (`422`) |
| Finish (`succeeded`, or `failed` with an optional `error_summary` ≤ 500 chars) | Only from `running`; a run with a failed task cannot succeed (`409`); repeating the same finish is `200`, anything else on a finished run `409` |

The database enforces the same shape: `finished_at` is set exactly when the
status is terminal, statuses and triggers are checked, and `(run, task_id)`
is unique. Messages come from the orchestrator and are stored as plain
text; logs record ids and statuses only.

### Run history

`GET /v1/workspaces/{id}/pipelines/{id}/runs` (any member) lists a
pipeline's runs, newest business date first. Each item is a summary: status,
trigger, business date, start/finish time, `duration_seconds` (null while
running), `error_summary` and `task_counts` (`succeeded` / `failed` /
`skipped`); `GET …/runs/{run_id}` returns the full run with every task.

| Query parameter | Meaning |
|---|---|
| `status` | `running`, `succeeded` or `failed` |
| `trigger` | `scheduled`, `manual` or `backfill` |
| `business_date_from`, `business_date_to` | Inclusive window; `from` after `to` is `422` |
| `limit` | 1–200, default 50 |
| `cursor` | `next_cursor` from the previous page; filters must stay the same |

Runs of the same business date (a retry, a manual re-run) come in a stable
order, so paging never skips or repeats a run. The index
`ix_pipeline_runs_history` (`pipeline_id, business_date, id`) backs this
order.

## Freshness rules

A dataset's `freshness_sla_minutes` is the promise shown in the registry; a
freshness rule says how DataHarbor measures it. One rule per dataset:

| Field | Meaning |
|---|---|
| `signal` | `landed_file` — the newest file landed for the dataset (raw datasets only); `pipeline_run` — the newest successful run of `pipeline_id` |
| `pipeline_id` | Required with `pipeline_run`, rejected otherwise; must be a pipeline of the same workspace (`422`) |
| `warn_after_minutes` | Optional; must be below `error_after_minutes` |
| `error_after_minutes` | 1–525600; defaults to the dataset's `freshness_sla_minutes` (`422` if the dataset has none) |

```http
PUT /v1/workspaces/{id}/datasets/{stg_orders}/freshness-rule
{"signal": "pipeline_run", "pipeline_id": "…", "warn_after_minutes": 1440}
```

`PUT` without `version` creates the rule (`201`; `409` if one exists); with
the `version` you read it replaces the rule (`200`, version + 1; `409` if
someone else changed it first).

Evaluation takes the time of the newest update (`landed_at` of a landed
file, `finished_at` of a succeeded run; failed and running runs do not
count) and its age at request time:

| Status | When |
|---|---|
| `fresh` | age ≤ `warn_after_minutes` (or ≤ `error_after_minutes` without a warn threshold) |
| `warning` | older than `warn_after_minutes`, not older than `error_after_minutes` |
| `stale` | older than `error_after_minutes` |
| `unknown` | never updated |

Thresholds are exclusive, so an update exactly `error_after_minutes` old is
not yet stale. Responses include `last_updated_at`, `last_business_date`,
`age_seconds` and `evaluated_at`. The workspace overview evaluates every
rule with two grouped queries, sorts `stale`, `unknown`, `warning`, `fresh`,
then by name, and reports `counts` for all rules even when `status`
filters the items. Rule changes are logged with dataset ids and versions
only.

## Data quality rules

Ingestion contracts decide whether a file may land (columns, types,
nullability, primary key). Quality rules test the values of landed files
and record the outcome; they never block landing. Each rule belongs to a
raw dataset, has a unique `name` (`^[a-z][a-z0-9_]{1,62}$`), a `severity`
(`warn` or `error`), a `description`, `enabled` and a `version`.

| `check` | Fields | A record fails when |
|---|---|---|
| `not_null` | one column | the value is null or empty |
| `unique` | 1–8 `columns` | its values repeat an earlier record's (records with a null key column are skipped) |
| `accepted_values` | one column, `values` (1–100) | the value is not listed (exact, case-sensitive) |
| `value_range` | one integer, decimal, date or timestamp column; `min` and/or `max` | the value is outside the inclusive range |
| `row_count` | `min` and/or `max` (whole numbers) | — the whole file fails when its record count is outside the range |

```http
POST /v1/workspaces/{id}/datasets/{raw_orders}/quality-rules
{"name": "quantity_plausible", "check": "value_range", "columns": ["quantity"],
 "min": "1", "max": "100", "severity": "warn"}
```

Bounds are strings, so decimals and dates compare exactly; timestamps need
an offset. A rule must fit the latest contract (`422` naming the unknown
column or wrong type). `PUT` replaces the rule and needs the `version` you
read (`409` otherwise); `DELETE` keeps past results, whose `rule_id`
becomes `null`.

`POST …/files/{file_id}/quality-checks` re-hashes the stored object (`409`
if it no longer matches its SHA-256, `503` if storage is down), reads it
once for all enabled rules and records one result per rule version:

| Result `status` | Meaning |
|---|---|
| `pass` | no failing records |
| `fail` | `failing_records` of `checked_records` failed; `sample_records` lists up to 20 record numbers (1-based) |
| `error` | the rule cannot run on this file, e.g. its column is not in the contract version the file landed under |

The file's `status` is `failed` when an `error` rule fails or errors,
`warning` when only `warn` rules do, `passed` otherwise and `no_rules`
without enabled rules. Results are written once: running again returns the
recorded results (`200`); a changed rule (new version) is evaluated again
(`201`). Concurrent runs record each result once. Results and logs carry
counts and record numbers only, never values.

History endpoints return the newest results first and filter by `status`
(repeatable), `severity`, `business_date`, `limit` (1–200), and on the
dataset route also `rule_id` and `landed_file_id`.

## Demo data

Seed a demo workspace (`acme-analytics`) with one synthetic user per role,
six datasets that mirror the synthetic sources in
[`packages/fixtures`](../../packages/fixtures) (`raw_*`, `stg_*`,
`daily_revenue`, owned by the engineer), a first ingestion contract for
each `raw_*` dataset that matches the fixture files, the
`ingest_raw_sources` pipeline (DAG `dataharbor_ingest_raw_sources`), a
`landed_file` freshness rule for each `raw_*` dataset (warning after 25 h,
stale after 26 h, since files are due daily at 03:00 UTC), nine data
quality rules on the `raw_*` datasets that the fixture files pass (known
segments, statuses and payment methods, non-negative amounts, plausible
quantities, unique emails, non-empty customer snapshot), and print a fresh
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
version and keeps datasets. `0005` adds `landed_files`; rolling it back
drops the file records only — objects stay in MinIO. `0006` adds
`pipelines`, `pipeline_runs` and `pipeline_run_tasks`; rolling it back drops
the run history. `0007` adds the run-history index; rolling it back drops
the index only. `0008` adds `freshness_rules`; rolling it back drops the
rules only (datasets keep their SLA). `0009` adds `quality_rules` and
`quality_results`; rolling it back drops the rules and their result
history only.

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
landing zone (stored bytes and keys, duplicate uploads, declared
checksums, contract failures, roles, MinIO outages, verify, no file
contents in logs; MinIO is replaced by an in-memory store), freshness
rules (threshold boundaries, signals, version conflicts, roles, workspace
scoping; the clock is pinned by overriding the `utc_now` dependency), the
seed command, and that the ORM models match the migrations. Two tests
generate files with [`packages/fixtures`](../../packages/fixtures) and
check that clean files pass the seeded contracts and that the duplicate
order, null payment amount and renamed column defects are caught; a
third lands every clean fixture file with its manifest SHA-256.
