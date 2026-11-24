# DataHarbor worker

Airflow DAGs (`dags/`) and the task logic behind them (`dataharbor_worker/`,
standard library only, so it runs unchanged in the stock
`apache/airflow:2.10.4-python3.12` image). Tasks report every run to the
DataHarbor API; see [ADR 0002](../../docs/adr/0002-airflow-run-tracking.md).

## `dataharbor_ingest_raw_sources`

Daily at 03:00 UTC, when the synthetic source files are due. Each run lands
one business date (Airflow `ds`):

```text
open_run ─┬─ land_raw_customers ─┐
          ├─ land_raw_orders ────┼─ close_run
          └─ land_raw_payments ──┘
```

| Task | Does |
|---|---|
| `open_run` | Opens (or, on a retry, reuses) the DataHarbor run for Airflow's `run_id`, business date and trigger (`scheduled`, `manual`, `backfill`) |
| `land_raw_<source>` | Finds the file for that source and date in the source directory's `manifest.json`, lands it through `POST …/datasets/{id}/files` with the manifest SHA-256 as `X-Content-SHA256`, and records the outcome: `succeeded` (with the landed file), `skipped` (no file for this date) or `failed` (contract violations, changed or missing file). Failures are not retried; API and network errors are |
| `close_run` | Always runs. Finishes the DataHarbor run as `succeeded`, or `failed` with a summary naming the failed or unreported tasks, and then fails the DAG run too |

The source directory has the layout written by
[`packages/fixtures`](../../packages/fixtures/README.md). Manifest paths that
point outside it are refused.

## dbt project

`dbt/` builds the warehouse models from the `raw` schema (ADR 0001 layout):

| Model | Schema | Materialized | Registry dataset | Contents |
|---|---|---|---|---|
| `stg_orders` | `staging` | view | `stg_orders` | Typed orders, one row per `order_id` (a re-delivered order keeps its latest business date); `order_date` in UTC |
| `stg_payments` | `staging` | view | `stg_payments` | Typed payments, one row per `payment_id`; `has_known_order = false` flags payments for orders not in `stg_orders` |
| `daily_revenue` | `mart` | table | `daily_revenue` | Per order date and currency: completed and paid orders, order / paid / outstanding amounts; orphan payments counted separately and never as revenue |

The `raw` tables are filled by `load-raw`:

```bash
python3 -m dataharbor_worker load-raw --business-date 2026-02-23
```

For each raw dataset it asks the API which file landed for that date, reads
the bytes from the source directory, and loads them only if their SHA-256
matches the landed file — files refused by their contract never reach the
warehouse. Columns are stored as `text`, as delivered, plus `_business_date`,
`_file_sha256`, `_record_number` and `_loaded_at`. A load replaces that date's
rows in one transaction, so reloading or backfilling a date is idempotent.
Prints one line per dataset (`loaded`, `skipped` when nothing landed, or
`failed`); exit code 1 if any failed.

Then build the models:

```bash
cd dbt
dbt build --profiles-dir .
```

`profiles.yml` reads the same `DATAHARBOR_WAREHOUSE_*` variables. Install the
extras with `pip install -e '.[warehouse,dbt]'`.

`dbt build` also runs the data tests and stops before any model downstream of
a failing one:

| Where | Tests |
|---|---|
| `stg_orders` | `order_id` unique and not null; required columns not null; `status` is `completed` or `cancelled`; `currency` is a three-letter code; quantities and amounts not negative; `amount = quantity × unit_price` |
| `stg_payments` | `payment_id` unique and not null; payments flagged as known have an order in `stg_orders`, and `has_known_order` agrees with `stg_orders` both ways |
| `daily_revenue` | one row per `revenue_date` and `currency`; counts and amounts not negative (a negative `outstanding_amount`, i.e. overpayment, only warns); `paid_orders ≤ completed_orders`; totals per currency reconcile with the staging models |

Custom generic tests live in `dbt/tests/generic/` (`non_negative`,
`currency_code`, `unique_combination`), singular ones in `dbt/tests/`. Running dbt from Airflow and
reporting it as a `transformation` pipeline run comes next.

## Settings

| Variable | Meaning |
|---|---|
| `DATAHARBOR_API_URL` | Base URL of the API |
| `DATAHARBOR_API_TOKEN` | Bearer token of an engineer in the workspace (never logged) |
| `DATAHARBOR_WORKSPACE_ID` | Workspace that owns the `ingest_raw_sources` pipeline |
| `DATAHARBOR_SOURCE_DIR` | Directory with `manifest.json` and the source files |
| `DATAHARBOR_API_TIMEOUT_SECONDS` | Per-request timeout (default 30) |
| `DATAHARBOR_WAREHOUSE_HOST`, `_PORT`, `_DB` | Warehouse PostgreSQL for `load-raw` and dbt (default `127.0.0.1`, `5432`, `warehouse`) |
| `DATAHARBOR_WAREHOUSE_USER`, `_PASSWORD` | Its credentials (password never logged) |

In Compose these come from `.env` (`DATAHARBOR_WORKER_*`,
`DATAHARBOR_SOURCE_DIR_HOST`); see [`infra/compose`](../../infra/compose/README.md).

## Run without Airflow

```bash
cd services/worker
export DATAHARBOR_API_URL=http://127.0.0.1:8000 DATAHARBOR_API_TOKEN=<engineer token> \
       DATAHARBOR_WORKSPACE_ID=<workspace id> DATAHARBOR_SOURCE_DIR=../../packages/fixtures/out/clean
python3 -m dataharbor_worker ingest --business-date 2026-02-23
```

Prints the finished run; exit code 1 if it failed.

## Tests

The tests drive the real API in-process (SQLite, in-memory object store), so
install both packages:

```bash
python3 -m venv .venv
.venv/bin/pip install -e '../api[test]' -e '.[test]'
.venv/bin/python -m pytest
```

`tests/test_dag.py` also parses the DAG when Airflow is installed and is
skipped otherwise. The warehouse loader tests use an in-memory stand-in; set
`DATAHARBOR_TEST_WAREHOUSE_USER`, `_PASSWORD` (and `_HOST`, `_PORT`, `_DB` as
needed) to also run them against a real PostgreSQL database — they drop and
recreate its `raw`, `staging` and `mart` schemas, so point them at a scratch
database.

`tests/test_dbt.py` runs dbt from the suite when a `dbt` executable is found
(on `PATH`, or set `DATAHARBOR_TEST_DBT`): `dbt parse` checks that every model
has tests; with the warehouse variables set it also lands the clean and the
defect fixtures, loads them and runs `dbt build` (all tests must pass, orphans
counted), and checks that a bad staged order fails the build:

```bash
.venv/bin/pip install -e '.[warehouse,dbt]'
DATAHARBOR_TEST_WAREHOUSE_USER=… DATAHARBOR_TEST_WAREHOUSE_PASSWORD=… .venv/bin/python -m pytest
```
