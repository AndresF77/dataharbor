# 0002. Airflow runs are tracked through the API

- **Status:** Accepted
- **Date:** 2026-11-23

## Context

Week 4 adds the first Airflow DAG: land each business date's raw source files.
DataHarbor must show every run with a stable ID and an execution history that
cannot be rewritten (a product invariant), and later features — run history,
incidents, backfills — build on those records. Airflow has its own metadata
database, but it is an implementation detail of the orchestrator, can be reset
locally, and knows nothing about workspaces, datasets or landed files.

Airflow retries tasks, and people clear and re-run them, so every step has to be
safe to repeat.

## Decision

- DataHarbor keeps its own `pipelines`, `pipeline_runs` and `pipeline_run_tasks`
  tables. A run is keyed by Airflow's `run_id` (unique per pipeline), so a
  retried "start" returns the existing run.
- DAG tasks talk to the **API over HTTP with an engineer token**, not to the
  database. Landing therefore goes through the same contract check, checksum
  and permission rules as any other client.
- The task logic lives in `services/worker/dataharbor_worker` (standard library
  only, so it runs in the stock Airflow image and in tests without Airflow).
  The DAG file only wires tasks together.
- A task outcome is written once; a run moves from `running` to `succeeded` or
  `failed` once. Retries send identical reports (messages are built from the
  file's own facts, not from whether this attempt was first), which the API
  accepts as no-ops.
- Contract violations fail the Airflow task without retries; API and network
  errors are retried. `close_run` always runs, finishes the DataHarbor run, and
  counts any task that never reported as failed.

## Consequences

- The API must be reachable from Airflow (Compose uses `host.docker.internal`)
  and the worker needs a token; a dedicated service identity comes later.
- If Airflow is reset, DataHarbor's history survives, and Airflow `run_id`s are
  reused only for the same date and trigger (otherwise `409`).
- dbt runs (next) will report through the same endpoints as a `transformation`
  pipeline.
