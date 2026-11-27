# Week 06 progress — freshness and data quality engine

| Day | Task | Status | Evidence |
|---|---|---|---|
| D36 | Acceptance cases and ADR | In progress | Acceptance cases below; freshness reuses the update signals the platform already records (landed files, pipeline runs), so no new ADR yet |
| D37 | Core data schema and migration | In progress | Alembic `0008` `freshness_rules` (one per dataset, signal CHECK, pipeline required exactly for `pipeline_run`, `warn < error`, pipeline delete restricted); rollback drops the rules only |
| D38 | API endpoint and validation | In progress | `GET/PUT/DELETE …/datasets/{id}/freshness-rule`, `GET …/datasets/{id}/freshness`, `GET /v1/workspaces/{id}/freshness` ([API README](../../services/api/README.md#freshness-rules)) |
| D39 | UI workflow with error states | Not started | |
| D40 | Unit and integration tests | In progress | `tests/test_freshness.py`: threshold boundaries, signals, version conflicts, roles, workspace scoping; migration CHECK tests |
| D41 | Observability and security checks | In progress | Rule changes log dataset id, version and signal only; non-members get `404`, viewers cannot change rules |
| D42 | Demo, regressions and next risks | Not started | |

## Acceptance cases

| ID | Given | When | Then |
|---|---|---|---|
| AC-06.1 | A raw dataset with a `landed_file` rule (warn 25 h, stale 26 h) | a file lands and 10 min / 25 h + 1 s / 26 h + 1 s pass | the dataset is `fresh` / `warning` / `stale`, with `age_seconds`, `last_updated_at` and `last_business_date` of the newest file |
| AC-06.2 | A stale raw dataset | a newer file lands | it is `fresh` again and reports the newer business date |
| AC-06.3 | A staging dataset with a `pipeline_run` rule | the pipeline has only failed or running runs | the dataset is `unknown`; after a successful run its age counts from that run's `finished_at` |
| AC-06.4 | An update exactly `error_after_minutes` old | freshness is evaluated | it is not yet `stale` (thresholds are exclusive) |
| AC-06.5 | A rule without `error_after_minutes` | it is saved | the dataset's `freshness_sla_minutes` is used; without an SLA → `422` |
| AC-06.6 | Invalid rules: no pipeline for `pipeline_run`, a pipeline for `landed_file`, a pipeline of another workspace, `landed_file` on a non-raw dataset, `warn ≥ error`, out-of-range minutes | `PUT` | `422`, nothing saved |
| AC-06.7 | An existing rule | `PUT` without `version`, or with an old one | `409`; with the current version the rule is replaced and its version goes up; eight concurrent first `PUT`s create exactly one rule |
| AC-06.8 | Datasets with rules in two workspaces | the workspace overview is read | only the caller's workspace is listed, `stale` → `unknown` → `warning` → `fresh`, then by name; `counts` cover every rule even when `status` filters the items |
| AC-06.9 | A viewer, a non-member, no token | they read or change a rule | viewer reads (`200`) but cannot change (`403`); non-member `404`; no token `401` |

## Notes

- Verified with SQLite (API suite) and on PostgreSQL 17: `0008` upgrade,
  downgrade to `0007` and upgrade again, seed twice (three rules), models
  match the migrations, 8 concurrent first `PUT`s → one `201` and seven
  `409`, a pipeline a rule watches cannot be deleted, evaluation and the
  overview with a pinned clock. Not run in the Compose stack (no Docker on
  the build machine).
- The transformation (dbt) pipeline is not registered yet, so the seed only
  adds rules for the raw datasets; staging and mart rules come once dbt
  runs are reported.
