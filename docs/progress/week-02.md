# Week 02 progress — Workspace permissions and dataset registry

| Day | Task | Status | Evidence |
|---|---|---|---|
| D08 | API service, health checks and migration tooling | Done | [`services/api`](../../services/api/README.md): `/health`, `/health/ready`, request IDs, error envelope, Alembic `0001` (`workspaces`); 14 tests pass |
| D09 | Core data schema and migration | In progress | `workspaces` table and constraints; members and roles next |
| D10 | API endpoint and validation | Not started | |
| D11 | UI workflow with error states | Not started | |
| D12 | Unit and integration tests | Not started | |
| D13 | Observability and security checks | Not started | |
| D14 | Demo, regressions and next risks | Not started | |

## Notes

- Tests use a throwaway SQLite database per test. The PostgreSQL DDL was
  checked with `alembic upgrade head --sql`, but the migration has not yet
  been applied to the Compose PostgreSQL (Docker is not available on the
  build machine).
