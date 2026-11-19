# Week 03 progress — Ingestion contracts and landing zone

| Day | Task | Status | Evidence |
|---|---|---|---|
| D15 | Acceptance cases and ADR | Not started | |
| D16 | Core data schema and migration | Done | Alembic `0004`: `ingestion_contracts` (versioned per dataset, format check, JSON columns and primary key, creator); rollback drops contracts and keeps datasets |
| D17 | API endpoint and validation | Done | [`services/api`](../../services/api/README.md#ingestion-contracts): publish (raw datasets only, `based_on_version` → `409` when stale), latest/version list/detail, `validate` sample files against a contract version with eight rules; demo seed publishes contracts for `raw_customers`, `raw_orders`, `raw_payments`; 114 tests pass |
| D18 | UI workflow with error states | Not started | |
| D19 | Unit and integration tests | In progress | Clean fixture files pass the seeded contracts; duplicate order IDs, null payment amounts and the renamed column are reported |
| D20 | Observability and security checks | Not started | Validation reports never echo source values |
| D21 | Demo, regressions and next risks | Not started | |

## Notes

- Orphan payments and late files are not contract violations: they need
  other files or arrival times and are checked after landing.
- Landing files in MinIO with checksums is next.
- PostgreSQL DDL for `0004` checked with `alembic upgrade 0003:0004 --sql`;
  not yet applied to the Compose PostgreSQL (no Docker on the build machine).
