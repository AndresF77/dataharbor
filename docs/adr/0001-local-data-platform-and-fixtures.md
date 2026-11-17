# 0001. Local data platform and synthetic fixture strategy

- **Status:** Accepted
- **Date:** 2026-11-17

## Context

DataHarbor tracks datasets, pipeline runs, dbt models, quality checks and lineage
for small analytics teams. The signature demo loads sample orders and payments,
schedules ingestion, transforms them with dbt, fails a quality check, traces the
affected downstream datasets and replays a backfill.

The whole demo has to run offline on one machine, be repeatable from scratch, and
use only synthetic data. Two early choices shape the rest of the project:

1. Which local services make up the data platform, and who owns which state.
2. Where the source data comes from and how it stays reproducible.

## Decision

### Local data platform

All services run through Docker Compose (`infra/compose/`) with pinned image tags:

| Service | Role |
|---|---|
| PostgreSQL | Two databases: `dataharbor` (application metadata: workspaces, datasets, runs, rules, lineage, audit) and `warehouse` (raw, staging and mart schemas that dbt builds). Airflow keeps its own metadata database on the same server. |
| MinIO | S3-compatible object store for landed source files. One bucket per environment, keys partitioned by `source/date/`. |
| Airflow | Orchestrates ingestion and dbt runs. DAGs live in `services/worker/dags/`. |
| dbt Core | Runs inside Airflow tasks against the `warehouse` database. Project lives in `services/worker/dbt/`. |
| Redis | Queue and short-lived cache for the API (run summaries, notifications). Never authoritative. |

Ownership rules:

- **The FastAPI service (`services/api`) is the only writer of application
  metadata.** Airflow tasks report run and task status back through the API, not
  by writing to the metadata tables directly.
- **Landed files are immutable.** A file in MinIO is written once with its SHA-256
  checksum recorded; reprocessing creates a new run, never an overwrite.
- **Lineage comes from observed artifacts.** Edges are derived from the dbt
  `manifest.json` and declared ingestion contracts, not entered by hand.
- **Backfills are partition-bounded.** Every load is keyed by source and business
  date, so replaying a date range is idempotent.

### Synthetic source fixtures

- **Generated, not hand-written.** A Python generator in `packages/fixtures`
  produces the source files from a fixed seed and a fixed reference date. The same
  seed always produces byte-identical output.
- **Versioned.** Each fixture set carries a `fixture_version`; run records and
  test snapshots store the version they used.
- **Plainly fake.** Customer, merchant and product names are invented. No real
  people, companies, card numbers or account identifiers.
- **Shaped like real sources.** Orders arrive as daily CSV files, payments as
  newline-delimited JSON, and customers as a CSV snapshot, so each connector type
  is exercised.
- **Defects on purpose.** Alongside a clean set, the generator can emit known
  defects for the quality-check demo: a late file (freshness), duplicate order IDs,
  payments that reference unknown orders, null amounts and a renamed column
  (schema drift). Each defect is listed in the fixture manifest so tests can
  assert exactly which checks should fail.

## Consequences

- The demo and CI can rebuild the full platform from nothing with one command and
  a seed, and failures are reproducible.
- Airflow and dbt add heavy local dependencies; the API and its tests must keep
  working without them (Airflow is mocked in API tests).
- Keeping run status flowing through the API adds one hop, but it means
  authorization, auditing and idempotency are enforced in one place.
- The generator has to evolve with the ingestion contracts; schema changes need a
  new `fixture_version`.

## Alternatives considered

- **Cloud warehouse and object storage.** Rejected: needs accounts and credentials,
  and breaks the offline demo.
- **DuckDB instead of PostgreSQL for the warehouse.** Simpler locally, but splits
  the stack across two SQL engines; may be revisited for local tests.
- **Cron or a custom scheduler instead of Airflow.** Rejected: run history, retries
  and backfills are core features, and Airflow already models them.
- **Public sample datasets.** Rejected: they cannot produce controlled defects on
  demand, and their licenses vary.
