# Week 05 progress — dbt transformations and tests

| Day | Task | Status | Evidence |
|---|---|---|---|
| D29 | Acceptance cases and ADR | In progress | Acceptance cases below; the layout follows [ADR 0001](../adr/0001-local-data-platform-and-fixtures.md) (raw / staging / mart in the `warehouse` database, dbt project in `services/worker/dbt`) |
| D30 | Core data schema and migration | Done | `raw.customers`, `raw.orders`, `raw.payments` created by the loader (text columns + load metadata, primary key `(_business_date, _record_number)`); dbt models `stg_orders`, `stg_payments` (views) and `daily_revenue` (table) |
| D31 | API endpoint and validation | Not started | |
| D32 | UI workflow with error states | Not started | |
| D33 | Unit and integration tests | Done | Loader: parsing, partition replacement and rollback, landed-file gate, checksum refusal, no record values in logs; PostgreSQL test (idempotent reload per date) runs when `DATAHARBOR_TEST_WAREHOUSE_*` is set. dbt: column, generic and singular data tests on all three models; `tests/test_dbt.py` runs `dbt parse` and, with a warehouse, `dbt build` on clean and defect fixtures (AC-05.7–9) |
| D34 | Observability and security checks | In progress | Loader logs table, date, row count and checksum prefix only |
| D35 | Demo, regressions and next risks | Not started | |

## Acceptance cases

| ID | Given | When | Then |
|---|---|---|---|
| AC-05.1 | A business date whose files landed | `load-raw` runs for it | each landed file is in its `raw.*` table with one row per record and the file's SHA-256; datasets with nothing landed are `skipped` |
| AC-05.2 | A date already loaded | `load-raw` runs again (or a backfill reloads it) | that date's rows are replaced, other dates are untouched, row counts do not grow |
| AC-05.3 | A file the contract refused at landing (e.g. `amount` renamed) | `load-raw` runs | nothing is loaded for that dataset and date |
| AC-05.4 | A source file changed after it landed | `load-raw` runs | the dataset fails with a checksum message; nothing is loaded |
| AC-05.5 | Raw tables for all clean fixture dates | `dbt build` runs | `stg_orders`, `stg_payments` and `daily_revenue` build; every captured payment matches an order, nothing is outstanding |
| AC-05.6 | Defect fixtures (orphan payments, a missing orders file) | `dbt build` runs | the orphan payments are flagged in `stg_payments` and counted in `orphan_payments`, not in `paid_amount`; days whose orders file was refused show their payments as orphans |
| AC-05.7 | Raw tables for the clean or the defect fixtures | `dbt build` runs | every data test passes: unique ids, accepted statuses, currency codes, non-negative amounts, `amount = quantity × unit_price`, orphan flag consistent with orders, mart one row per date and currency and reconciled with staging |
| AC-05.8 | A staged order with an unknown status, a bad currency code and a wrong total | `dbt build` runs | the matching tests fail, the build exits non-zero and `daily_revenue` is skipped |
| AC-05.9 | The worker test suite with dbt installed | `pytest` runs | the dbt project parses with tests on every model; with `DATAHARBOR_TEST_WAREHOUSE_*` set the builds above run too |

## Notes

- Checked locally against PostgreSQL 17 with dbt-core 1.9.1 / dbt-postgres
  1.9.0: all fixture dates landed through the API (SQLite, in-memory object
  store), loaded with `load-raw`, then `dbt build` (3 models). Clean set:
  7 days, 162 completed orders, all paid. Defect set: 2026-02-24 and
  2026-02-27 orders refused at landing, so their 24 and 21 payments show as
  orphans; 2026-02-25 has 2 orphan payments (251.35 USD) and the matching
  outstanding amount; 2026-02-26 payments refused (null amounts), so the whole
  day is outstanding. Not yet run in the Compose stack (no Docker on the build
  machine).
- The loader reads the bytes from the source directory, gated by the landed
  file's checksum; reading straight from the landing bucket needs storage
  credentials in the worker and is left for later.
