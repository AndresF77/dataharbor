"""Load landed raw files into the warehouse `raw` schema for dbt.

dbt reads three tables, one per raw dataset:

    raw.customers, raw.orders, raw.payments

Every source column is stored as `text`, exactly as delivered; typing,
renaming and de-duplication happen in the dbt staging models. Each row also
carries load metadata: `_business_date`, `_file_sha256` (the landed file it
came from), `_record_number` (1-based, as in contract reports) and
`_loaded_at`.

Loads are partition-bounded: loading a dataset for a business date replaces
that date's rows in one transaction, so re-running or backfilling a date is
idempotent and never touches other dates.

Only files that passed the landing zone are loaded. The worker asks the API
which file landed for the dataset and date, reads the bytes from the source
directory, and refuses them unless their SHA-256 matches the landed file.

The warehouse connection is any DB-API connection with the `%s`
parameter style (psycopg 3 locally, psycopg2 inside the Airflow image).
Nothing here logs record values.
"""

import csv
import hashlib
import io
import json
import logging
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path, PurePosixPath
from typing import Any

from dataharbor_worker.api_client import ApiClient
from dataharbor_worker.ingestion import (
    RAW_DATASETS,
    SourceError,
    read_source,
    entry_for,
    load_manifest,
)

logger = logging.getLogger(__name__)

RAW_SCHEMA = "raw"


@dataclass(frozen=True)
class RawTable:
    dataset: str
    table: str
    fmt: str
    columns: tuple[str, ...]

    @property
    def qualified(self) -> str:
        return f"{RAW_SCHEMA}.{self.table}"


# Column order matches the first contract version of each raw dataset.
RAW_TABLES: dict[str, RawTable] = {
    t.dataset: t
    for t in (
        RawTable("raw_customers", "customers", "csv",
                 ("customer_id", "customer_name", "email", "country", "segment", "signup_date")),
        RawTable("raw_orders", "orders", "csv",
                 ("order_id", "customer_id", "merchant", "product", "quantity", "unit_price",
                  "amount", "currency", "status", "ordered_at")),
        RawTable("raw_payments", "payments", "jsonl",
                 ("payment_id", "order_id", "method", "amount", "currency", "status", "paid_at")),
    )
}
assert set(RAW_TABLES) == set(RAW_DATASETS)

METADATA_COLUMNS = ("_business_date", "_file_sha256", "_record_number")


class LoadError(Exception):
    """A file cannot be loaded into the warehouse."""


def parse_records(table: RawTable, data: bytes) -> list[tuple[str | None, ...]]:
    """Source records as tuples of text in `table.columns` order (`None` for nulls)."""
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise LoadError(f"{table.dataset}: file is not UTF-8") from None

    if table.fmt == "csv":
        reader = csv.reader(io.StringIO(text, newline=""))
        header = next(reader, None)
        if header is None or tuple(header) != table.columns:
            raise LoadError(f"{table.dataset}: header does not match the raw table columns")
        records = []
        for number, row in enumerate(reader, start=1):
            if len(row) != len(table.columns):
                raise LoadError(f"{table.dataset}: record {number} has {len(row)} fields")
            records.append(tuple(value if value != "" else None for value in row))
        return records

    records = []
    for number, line in enumerate((ln for ln in text.splitlines() if ln.strip()), start=1):
        try:
            obj = json.loads(line)
        except ValueError:
            raise LoadError(f"{table.dataset}: record {number} is not valid JSON") from None
        if not isinstance(obj, dict) or set(obj) != set(table.columns):
            raise LoadError(f"{table.dataset}: record {number} does not match the raw table columns")
        records.append(tuple(_text(obj[c]) for c in table.columns))
    return records


def _text(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return "true" if value else "false"
    return value if isinstance(value, str) else json.dumps(value)


def create_table_sql(table: RawTable) -> str:
    columns = ",\n    ".join(f"{c} text" for c in table.columns)
    return (
        f"CREATE TABLE IF NOT EXISTS {table.qualified} (\n"
        f"    {columns},\n"
        "    _business_date date NOT NULL,\n"
        "    _file_sha256 char(64) NOT NULL,\n"
        "    _record_number integer NOT NULL,\n"
        "    _loaded_at timestamptz NOT NULL DEFAULT now(),\n"
        f"    PRIMARY KEY (_business_date, _record_number)\n"
        ")"
    )


def ensure_raw_tables(conn) -> None:
    cur = conn.cursor()
    try:
        cur.execute(f"CREATE SCHEMA IF NOT EXISTS {RAW_SCHEMA}")
        for table in RAW_TABLES.values():
            cur.execute(create_table_sql(table))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        cur.close()


def replace_partition(conn, table: RawTable, business_date: date, file_sha256: str,
                      records: Sequence[tuple[str | None, ...]]) -> int:
    """Replace `table`'s rows for `business_date` with `records`, atomically."""
    columns = (*table.columns, *METADATA_COLUMNS)
    insert = (
        f"INSERT INTO {table.qualified} ({', '.join(columns)}) "
        f"VALUES ({', '.join(['%s'] * len(columns))})"
    )
    rows = [(*record, business_date, file_sha256, number)
            for number, record in enumerate(records, start=1)]
    cur = conn.cursor()
    try:
        cur.execute(f"DELETE FROM {table.qualified} WHERE _business_date = %s", (business_date,))
        if rows:
            cur.executemany(insert, rows)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        cur.close()
    return len(rows)


@dataclass
class LoadOutcome:
    dataset: str
    status: str  # loaded | skipped | failed
    rows: int = 0
    file_sha256: str | None = None
    message: str = ""


def load_business_date(client: ApiClient, conn, source_dir: Path,
                       business_date: date) -> list[LoadOutcome]:
    """Load every raw dataset's landed file for `business_date` into `raw.*`."""
    ensure_raw_tables(conn)
    datasets = client.raw_datasets()
    entries = load_manifest(source_dir)
    return [_load_one(client, conn, Path(source_dir), entries, datasets.get(name), name,
                      business_date)
            for name in RAW_DATASETS]


def _load_one(client: ApiClient, conn, source_dir: Path, entries, dataset: Mapping | None,
              name: str, business_date: date) -> LoadOutcome:
    table = RAW_TABLES[name]
    if dataset is None:
        return LoadOutcome(name, "failed", message=f"Dataset {name} is not registered")
    landed = client.landed_files(dataset["id"], business_date)
    if not landed:
        return LoadOutcome(name, "skipped", message=f"No landed {name} file for {business_date}")
    latest = landed[0]  # newest landing first

    try:
        entry = entry_for(entries, name, business_date)
        data = read_source(source_dir, entry.path) if entry else None
    except SourceError as exc:
        return LoadOutcome(name, "failed", message=str(exc))
    if data is None:
        return LoadOutcome(name, "failed",
                           message=f"Landed {name} file for {business_date} is not in the "
                                   "source directory")
    actual = hashlib.sha256(data).hexdigest()
    if actual != latest["sha256"]:
        return LoadOutcome(name, "failed", file_sha256=actual,
                           message=f"{PurePosixPath(entry.path).name} does not match the landed "
                                   f"file (sha256 {latest['sha256'][:12]})")
    try:
        records = parse_records(table, data)
    except LoadError as exc:
        return LoadOutcome(name, "failed", file_sha256=actual, message=str(exc))

    count = replace_partition(conn, table, business_date, actual, records)
    logger.info("loaded %s business_date=%s rows=%d sha256=%s", table.qualified, business_date,
                count, actual[:12])
    return LoadOutcome(name, "loaded", rows=count, file_sha256=actual,
                       message=f"{count} rows into {table.qualified}")


WAREHOUSE_SETTINGS = {
    "host": ("DATAHARBOR_WAREHOUSE_HOST", "127.0.0.1"),
    "port": ("DATAHARBOR_WAREHOUSE_PORT", "5432"),
    "dbname": ("DATAHARBOR_WAREHOUSE_DB", "warehouse"),
    "user": ("DATAHARBOR_WAREHOUSE_USER", None),
    "password": ("DATAHARBOR_WAREHOUSE_PASSWORD", None),
}


def connection_settings(env: Mapping[str, str] | None = None) -> dict[str, str]:
    """Connection keywords from the same DATAHARBOR_WAREHOUSE_* variables dbt uses."""
    env = os.environ if env is None else env
    settings, missing = {}, []
    for key, (variable, default) in WAREHOUSE_SETTINGS.items():
        value = env.get(variable, "").strip() or default
        if value is None:
            missing.append(variable)
        else:
            settings[key] = value
    if missing:
        raise LoadError(f"Set {', '.join(missing)} (see services/worker/README.md)")
    return settings


def connect(env: Mapping[str, str] | None = None):
    """Open a warehouse connection with psycopg 3, or psycopg2 inside Airflow."""
    settings = connection_settings(env)
    try:
        import psycopg
    except ImportError:
        try:
            import psycopg2 as psycopg
        except ImportError:
            raise LoadError("Install psycopg (or psycopg2) to load the warehouse") from None
    return psycopg.connect(**settings)
