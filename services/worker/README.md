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

## Settings

| Variable | Meaning |
|---|---|
| `DATAHARBOR_API_URL` | Base URL of the API |
| `DATAHARBOR_API_TOKEN` | Bearer token of an engineer in the workspace (never logged) |
| `DATAHARBOR_WORKSPACE_ID` | Workspace that owns the `ingest_raw_sources` pipeline |
| `DATAHARBOR_SOURCE_DIR` | Directory with `manifest.json` and the source files |
| `DATAHARBOR_API_TIMEOUT_SECONDS` | Per-request timeout (default 30) |

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
skipped otherwise.
