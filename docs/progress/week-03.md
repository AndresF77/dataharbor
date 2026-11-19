# Week 03 progress — Ingestion contracts and landing zone

| Day | Task | Status | Evidence |
|---|---|---|---|
| D15 | Acceptance cases and ADR | Not started | |
| D16 | Core data schema and migration | Done | Alembic `0004`: `ingestion_contracts` (versioned per dataset, format check, JSON columns and primary key, creator); rollback drops contracts and keeps datasets |
| D17 | API endpoint and validation | Done | [`services/api`](../../services/api/README.md#ingestion-contracts): publish (raw datasets only, `based_on_version` → `409` when stale), latest/version list/detail, `validate` sample files against a contract version with eight rules; demo seed publishes contracts for `raw_customers`, `raw_orders`, `raw_payments`. [Landing zone](../../services/api/README.md#landing-zone): `POST …/files` checks the latest contract, stores passing files in MinIO under `business_date=…/<sha256>` keys, records them in `landed_files` (Alembic `0005`), recognises re-sent bytes, `verify` re-checks the stored SHA-256; 135 tests pass |
| D18 | UI workflow with error states | Not started | |
| D19 | Unit and integration tests | In progress | Clean fixture files pass the seeded contracts and land with their manifest SHA-256; duplicate order IDs, null payment amounts and the renamed column are reported. MinIO itself is not exercised yet (in-memory store in tests) |
| D20 | Observability and security checks | In progress | Validation reports never echo source values; landing logs carry ids, sizes and checksums only (tested) |
| D21 | Demo, regressions and next risks | Not started | |

## Notes

- Orphan payments and late files are not contract violations: they need
  other files or arrival times and are checked after landing.
- Landed objects are content-addressed; rolling back `0005` leaves them in
  MinIO. A failed database write after a successful upload leaves an
  unregistered object that the next retry reuses.
- PostgreSQL DDL for `0004` and `0005` checked with `alembic upgrade … --sql`;
  not yet applied to the Compose PostgreSQL (no Docker on the build machine).
