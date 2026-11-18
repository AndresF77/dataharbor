#!/usr/bin/env bash
# Runs once, when the PostgreSQL data volume is first initialised.
# Creates the warehouse and Airflow databases next to the `dataharbor`
# metadata database that the image creates from POSTGRES_DB.
set -euo pipefail

: "${AIRFLOW_DB_USER:?AIRFLOW_DB_USER is required}"
: "${AIRFLOW_DB_PASSWORD:?AIRFLOW_DB_PASSWORD is required}"

psql -v ON_ERROR_STOP=1 \
  --username "$POSTGRES_USER" \
  --dbname "$POSTGRES_DB" \
  --set airflow_user="$AIRFLOW_DB_USER" \
  --set airflow_password="$AIRFLOW_DB_PASSWORD" \
  --set owner="$POSTGRES_USER" <<'SQL'
CREATE DATABASE warehouse OWNER :"owner";
CREATE ROLE :"airflow_user" LOGIN PASSWORD :'airflow_password';
CREATE DATABASE airflow OWNER :"airflow_user";
SQL

psql -v ON_ERROR_STOP=1 \
  --username "$POSTGRES_USER" \
  --dbname warehouse <<'SQL'
CREATE SCHEMA raw;
CREATE SCHEMA staging;
CREATE SCHEMA mart;
COMMENT ON SCHEMA raw IS 'Landed source records, loaded as-is per source and business date';
COMMENT ON SCHEMA staging IS 'dbt staging models: typed, renamed, deduplicated';
COMMENT ON SCHEMA mart IS 'dbt marts used by dashboards and quality checks';
SQL
