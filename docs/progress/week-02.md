# Week 02 progress — Workspace permissions and dataset registry

| Day | Task | Status | Evidence |
|---|---|---|---|
| D08 | API service, health checks and migration tooling | Done | [`services/api`](../../services/api/README.md): `/health`, `/health/ready`, request IDs, error envelope, Alembic `0001` (`workspaces`); 14 tests pass |
| D09 | Core data schema and migration | Done | Alembic `0002`: `users`, hashed `api_tokens`, `workspace_members` with `viewer`/`engineer`/`admin` role check; Alembic `0003`: `datasets` (unique name per workspace, layer check, owner, freshness SLA, `version`) |
| D10 | API endpoint and validation | Done | `/v1/workspaces` and member routes with bearer auth and role checks (401/404/403/409); dataset registry list (layer filter, cursor pages)/create/detail/PATCH with optimistic `version` (409 on stale edits, 422 on non-member owners); demo seed registers six datasets; 73 tests pass |
| D11 | UI workflow with error states | Not started | |
| D12 | Unit and integration tests | Not started | |
| D13 | Observability and security checks | Not started | |
| D14 | Demo, regressions and next risks | Not started | |

## Notes

- Tests use a throwaway SQLite database per test. The PostgreSQL DDL was
  checked with `alembic upgrade head --sql`, but the migrations have not yet
  been applied to the Compose PostgreSQL (Docker is not available on the
  build machine).
