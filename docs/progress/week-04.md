# Week 04 progress — Airflow DAGs and run tracking

| Day | Task | Status | Evidence |
|---|---|---|---|
| D22 | Acceptance cases and ADR | Done | [ADR 0002](../adr/0002-airflow-run-tracking.md); acceptance cases below |
| D23 | Core data schema and migration | Done | Alembic `0006`: `pipelines`, `pipeline_runs` (unique Airflow run id per pipeline, `finished_at` set exactly when terminal), `pipeline_run_tasks` (one outcome per task); rollback drops the run history only |
| D24 | API endpoint and validation | Done | [Run tracking endpoints](../../services/api/README.md#pipeline-runs): start / task outcome / finish / run detail; [run history](../../services/api/README.md#run-history) with status, trigger and business-date filters, task counts and keyset pages (Alembic `0007` index) |
| D25 | UI workflow with error states | Not started | |
| D26 | Unit and integration tests | In progress | API: 21 run-tracking + 25 run-history tests (ordering, filters, paging without gaps or repeats, cursor/limit validation, access). Worker: ingestion against the real API in-process, with clean and defect fixture sets |
| D27 | Observability and security checks | In progress | Task messages bounded and built from counts, rule names and checksums; logs carry ids and statuses only (tested); manifest paths cannot leave the source directory |
| D28 | Demo, regressions and next risks | Not started | |

## Acceptance cases

| ID | Given | When | Then |
|---|---|---|---|
| AC-04.1 | Clean fixtures, first business date | the DAG runs | `land_raw_orders` and `land_raw_payments` succeed with a landed file each, `land_raw_customers` is skipped (snapshot only on the last date), the run succeeds |
| AC-04.2 | A finished run | the same Airflow run is executed again | same DataHarbor run ID, same task outcomes, no new landed files |
| AC-04.3 | Defect fixtures, 5th business date (`amount` renamed) | the DAG runs | `land_raw_orders` fails with the contract rules (`missing_column`, `unexpected_column`) and no file is stored; the run fails with `1 task(s) failed: land_raw_orders` |
| AC-04.4 | Defect fixtures, 2nd / 4th date | the DAG runs | duplicate order IDs (`duplicate_key`) / null payment amounts (`null_value`) fail their task |
| AC-04.5 | A source file changed after the manifest was written | the DAG runs | the API refuses it via `X-Content-SHA256`; the task fails |
| AC-04.6 | A task never reported (crashed before recording) | `close_run` runs | the run fails and names the task |
| AC-04.7 | A finished run | anyone reports a task or a different finish | `409`; history unchanged |
| AC-04.8 | A viewer token | it tries to start a run | `403` |
| AC-04.9 | Runs over several dates, one date retried | a viewer pages through the history 2 at a time | newest date first, both attempts of the retried date listed once each, every run seen exactly once |
| AC-04.10 | A failed run and a running run | the history is read | the failed run shows its task counts and duration; the running one has no `finished_at` or duration |

## Notes

- `airflow dags test` was run locally with Airflow 2.10.4 against the API
  (uvicorn, SQLite, in-memory object store): AC-04.1 and AC-04.3 behave as
  above. Not yet run in the Compose stack (no Docker on the build machine).
- Airflow is not a test dependency; `tests/test_dag.py` parses the DAG only
  when Airflow is installed.
