# Week 01 acceptance cases: local data platform and fixtures

Scope: the local services and the synthetic source data generator described in
[ADR 0001](../adr/0001-local-data-platform-and-fixtures.md).
Each case is written as Given / When / Then and must be verifiable from a clean
checkout.

## Local data platform

**AC-01.1 — Services start healthy**
- Given Docker is available and `.env` is copied from `.env.example`
- When the Compose stack in `infra/compose` is started
- Then PostgreSQL, MinIO, Redis and the Airflow webserver and scheduler report
  healthy, and every image tag is pinned to a specific version

**AC-01.2 — Databases and bucket provisioned**
- Given the stack has started for the first time
- When the init scripts have run
- Then PostgreSQL has the `dataharbor`, `warehouse` and `airflow` databases, the
  `warehouse` database has `raw`, `staging` and `mart` schemas, and MinIO has the
  landing bucket named in `.env.example`

**AC-01.3 — Rebuild from nothing**
- Given the stack has been stopped and its volumes removed
- When it is started again
- Then it comes back to the same state as AC-01.2 with no manual steps

**AC-01.4 — No secrets committed**
- Given the repository
- When it is searched for credentials
- Then only placeholder values in `.env.example` exist, and `.env` files are
  ignored by git

**AC-01.5 — Services stay local**
- Given the Compose file
- Then service ports are bound to `127.0.0.1` only, and no service needs network
  access beyond pulling its image

## Synthetic source data generator

**AC-01.6 — Deterministic output**
- Given the default seed and reference date
- When the generator runs twice into two empty directories
- Then both runs produce byte-identical files

**AC-01.7 — Different seed, different data**
- Given two different seeds
- When the generator runs with each
- Then the outputs differ, and both pass the same validation checks

**AC-01.8 — Source shapes**
- Given a generated set
- Then orders are daily CSV files (one per business date), payments are
  newline-delimited JSON per business date, and customers are a single CSV
  snapshot, each with a documented, stable column order

**AC-01.9 — Manifest**
- Given any generated set
- When its manifest is read
- Then it states the `fixture_version`, seed, reference date, the business date
  range, and for every file its path, record count and SHA-256 checksum

**AC-01.10 — Referential integrity of the clean set**
- Given the clean set
- Then every order references an existing customer, every payment references an
  existing order, order IDs are unique, and every amount is a positive decimal
  with two places

**AC-01.11 — Plainly fictional**
- Given any generated customer, merchant or product
- Then names come from the generator's word lists, emails use `example.com` or
  `example.org`, and there are no card numbers, bank accounts or other real
  payment identifiers

**AC-01.12 — Defects on demand**
- Given the generator is run with defects enabled
- Then the set contains a late orders file, duplicate order IDs, payments that
  reference unknown orders, null payment amounts and a renamed column, and each
  defect is listed in the manifest with the file, the affected records and the
  check that must catch it

**AC-01.13 — Defects isolated**
- Given the clean set and the defect set for the same seed
- When they are compared
- Then only the files listed in the defect manifest differ

## Out of scope this week

- The FastAPI service, migrations and roles (week 2)
- Loading fixtures into MinIO or the warehouse (weeks 2–3)
- Airflow DAGs and dbt models (week 3)
- Performance-sized datasets

## Risks and open questions

- Airflow is the heaviest service; its startup time and memory use may make the
  local stack slow on small machines. A trimmed profile may be needed.
- The fixture schema must change together with the ingestion contracts from
  week 2 on; incompatible changes bump `fixture_version`.
- Defects must be detectable by the planned checks without making the clean set
  unrealistically tidy.
- Business dates and late arrival depend on time zones; generated timestamps stay
  in UTC with explicit offsets.
