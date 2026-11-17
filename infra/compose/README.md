# Local platform

Docker Compose stack for the services in
[ADR 0001](../../docs/adr/0001-local-data-platform-and-fixtures.md).

| Service | Image | Host port (127.0.0.1) |
|---|---|---|
| PostgreSQL (`dataharbor`, `warehouse`, `airflow` databases) | `postgres:16.6-bookworm` | `POSTGRES_PORT` (5432) |
| MinIO API / console | `minio/minio:RELEASE.2024-12-18T13-15-44Z` | `MINIO_API_PORT` (9000) / `MINIO_CONSOLE_PORT` (9001) |
| Redis | `redis:7.4.1-bookworm` | `REDIS_PORT` (6379) |
| Airflow webserver + scheduler (LocalExecutor) | `apache/airflow:2.10.4-python3.12` | `AIRFLOW_PORT` (8080) |

Two one-shot services run on start: `minio-init` creates the landing bucket and
`airflow-init` migrates the Airflow database and creates the admin user.

## Usage

From the repository root:

```bash
cp .env.example .env          # then change the passwords; set AIRFLOW_UID=$(id -u) on Linux
docker compose --env-file .env -f infra/compose/compose.yaml up -d
docker compose --env-file .env -f infra/compose/compose.yaml ps
```

To rebuild from nothing (this drops all local data), run `down -v` with the same
`--env-file` and `-f` flags, then `up -d` again.

The PostgreSQL init script (`postgres/init/01-databases.sh`) only runs when the
data volume is empty, so a password change in `.env` after the first start needs
a volume reset or a manual `ALTER ROLE`.

DAGs are mounted from `services/worker/dags/`.
