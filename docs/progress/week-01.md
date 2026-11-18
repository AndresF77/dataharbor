# Week 01 progress — Local data platform and fixtures

| Day | Task | Status | Evidence |
|---|---|---|---|
| D01 | Acceptance cases and ADR | Done | [ADR 0001](../adr/0001-local-data-platform-and-fixtures.md), [acceptance cases](../acceptance/week-01-local-platform-and-fixtures.md) |
| D02 | Core data schema and migration | Not started | |
| D03 | API endpoint and validation | Not started | |
| D04 | UI workflow with error states | Not started | |
| D05 | Unit and integration tests | Not started | |
| D06 | Observability and security checks | Not started | |
| D07 | Demo, regressions and next risks | Not started | |

## Notes

- Local services: Compose stack in [`infra/compose`](../../infra/compose/README.md)
  (AC-01.1–5).
- Synthetic source data: generator and checker in
  [`packages/fixtures`](../../packages/fixtures/README.md); 17 tests cover AC-01.6–13,
  including 25 seeds for validation, defect detection and defect isolation.
