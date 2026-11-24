import json
import logging
import re

import pytest

from dataharbor_worker.ingestion import entry_for, ingest, load_manifest
from dataharbor_worker.warehouse import (
    RAW_TABLES,
    LoadError,
    connection_settings,
    create_table_sql,
    load_business_date,
    parse_records,
    replace_partition,
)

RUN_ID = "scheduled__2026-02-24T03:00:00+00:00"
ORDERS = RAW_TABLES["raw_orders"]
PAYMENTS = RAW_TABLES["raw_payments"]


class FakeWarehouse:
    """DB-API stand-in that keeps each raw table's rows by business date."""

    def __init__(self):
        self.tables: dict[str, dict] = {}
        self.commits = 0
        self.rollbacks = 0
        self.fail_inserts = False
        self._pending: dict[str, dict] | None = None

    def cursor(self):
        return FakeCursor(self)

    def _state(self) -> dict[str, dict]:
        if self._pending is None:
            self._pending = {t: dict(parts) for t, parts in self.tables.items()}
        return self._pending

    def commit(self):
        if self._pending is not None:
            self.tables = self._pending
        self._pending = None
        self.commits += 1

    def rollback(self):
        self._pending = None
        self.rollbacks += 1

    def rows(self, table: str) -> list[tuple]:
        return [row for part in self.tables.get(table, {}).values() for row in part]


class FakeCursor:
    def __init__(self, db: FakeWarehouse):
        self.db = db

    def execute(self, sql, params=()):
        if sql.startswith("CREATE TABLE"):
            table = re.search(r"EXISTS (\S+)", sql).group(1)
            self.db._state().setdefault(table, {})
        elif sql.startswith("DELETE"):
            table = re.search(r"FROM (\S+)", sql).group(1)
            self.db._state()[table].pop(params[0], None)

    def executemany(self, sql, rows):
        if self.db.fail_inserts:
            raise RuntimeError("insert failed")
        table = re.search(r"INTO (\S+)", sql).group(1)
        for row in rows:
            self.db._state()[table].setdefault(row[-3], []).append(tuple(row))

    def close(self):
        pass


def test_parse_csv_keeps_text_and_maps_empty_to_null():
    data = ("﻿" + ",".join(ORDERS.columns) + "\r\n"
            "ORD-1,CUS-1,Shop,Tea,2,1.50,3.00,EUR,completed,2026-02-23T10:00:00+00:00\r\n"
            "ORD-2,CUS-2,Shop,,1,4.00,4.00,EUR,cancelled,2026-02-23T11:00:00+00:00\r\n").encode()

    records = parse_records(ORDERS, data)

    assert records[0] == ("ORD-1", "CUS-1", "Shop", "Tea", "2", "1.50", "3.00", "EUR",
                          "completed", "2026-02-23T10:00:00+00:00")
    assert records[1][3] is None


def test_parse_jsonl_orders_fields_by_table_columns():
    line = {"paid_at": "2026-02-23T10:01:00+00:00", "payment_id": "PAY-1", "order_id": "ORD-1",
            "method": "card", "amount": "3.00", "currency": "EUR", "status": "captured"}
    records = parse_records(PAYMENTS, (json.dumps(line) + "\n\n").encode())

    assert records == [("PAY-1", "ORD-1", "card", "3.00", "EUR", "captured",
                        "2026-02-23T10:01:00+00:00")]


@pytest.mark.parametrize(
    "table, data, message",
    [
        (ORDERS, b"order_id,customer_id\nORD-1,CUS-1\n", "header"),
        (ORDERS, (",".join(ORDERS.columns) + "\nORD-1,CUS-1\n").encode(), "record 1 has 2 fields"),
        (PAYMENTS, b'{"payment_id": "PAY-1"}\n', "record 1 does not match"),
        (PAYMENTS, b"not json\n", "record 1 is not valid JSON"),
        (PAYMENTS, b"\xff\xfe", "not UTF-8"),
    ],
)
def test_parse_rejects_files_that_do_not_fit_the_table(table, data, message):
    with pytest.raises(LoadError, match=message):
        parse_records(table, data)


def test_raw_tables_are_text_with_load_metadata():
    sql = create_table_sql(ORDERS)

    assert sql.startswith("CREATE TABLE IF NOT EXISTS raw.orders")
    assert "amount text" in sql and "ordered_at text" in sql
    assert "_file_sha256 char(64) NOT NULL" in sql
    assert "PRIMARY KEY (_business_date, _record_number)" in sql


def test_replace_partition_rolls_back_on_failure():
    db = FakeWarehouse()
    db.cursor().execute("CREATE TABLE IF NOT EXISTS raw.orders")
    db.commit()
    replace_partition(db, ORDERS, "2026-02-23", "a" * 64, [("ORD-1",) * 10])

    db.fail_inserts = True
    with pytest.raises(RuntimeError):
        replace_partition(db, ORDERS, "2026-02-23", "b" * 64, [("ORD-2",) * 10])

    assert db.rollbacks == 1
    assert [row[0] for row in db.rows("raw.orders")] == ["ORD-1"]


def test_landed_files_are_loaded_per_business_date(worker_client, make_sources):
    sources, options = make_sources()
    first, second = options.business_dates[:2]
    for day in (first, second):
        ingest(worker_client, sources, day, f"{RUN_ID}-{day}")
    db = FakeWarehouse()

    outcomes = {o.dataset: o for o in load_business_date(worker_client, db, sources, first)}

    entry = entry_for(load_manifest(sources), "raw_orders", first)
    assert outcomes["raw_orders"].status == "loaded"
    assert outcomes["raw_orders"].rows == entry.record_count
    assert outcomes["raw_orders"].file_sha256 == entry.sha256
    assert outcomes["raw_payments"].status == "loaded"
    assert outcomes["raw_customers"].status == "skipped"  # snapshot only on the last date
    orders = db.rows("raw.orders")
    assert len(orders) == entry.record_count
    assert {row[-3] for row in orders} == {first}
    assert [row[-1] for row in orders] == list(range(1, entry.record_count + 1))

    load_business_date(worker_client, db, sources, second)
    load_business_date(worker_client, db, sources, first)  # reload: replaces, never duplicates
    assert len(db.rows("raw.orders")) == entry.record_count + entry_for(
        load_manifest(sources), "raw_orders", second).record_count


def test_file_rejected_at_landing_is_not_loaded(worker_client, make_sources):
    sources, options = make_sources(defects=True)
    renamed_date = options.business_dates[4]  # `amount` renamed: refused by the contract
    ingest(worker_client, sources, renamed_date, RUN_ID)
    db = FakeWarehouse()

    outcomes = {o.dataset: o for o in load_business_date(worker_client, db, sources,
                                                         renamed_date)}

    assert outcomes["raw_orders"].status == "skipped"
    assert db.rows("raw.orders") == []
    assert outcomes["raw_payments"].status == "loaded"


def test_source_changed_after_landing_is_refused(worker_client, make_sources):
    sources, options = make_sources()
    day = options.business_dates[0]
    ingest(worker_client, sources, day, RUN_ID)
    path = sources / entry_for(load_manifest(sources), "raw_orders", day).path
    path.write_bytes(path.read_bytes().replace(b"completed", b"cancelled", 1))
    db = FakeWarehouse()

    outcomes = {o.dataset: o for o in load_business_date(worker_client, db, sources, day)}

    assert outcomes["raw_orders"].status == "failed"
    assert "does not match the landed file" in outcomes["raw_orders"].message
    assert db.rows("raw.orders") == []


def test_load_logs_carry_no_record_values(worker_client, make_sources, caplog):
    sources, options = make_sources()
    day = options.business_dates[0]
    ingest(worker_client, sources, day, RUN_ID)

    with caplog.at_level(logging.DEBUG):
        load_business_date(worker_client, FakeWarehouse(), sources, day)

    assert "rows=" in caplog.text
    assert "ORD-" not in caplog.text and "PAY-" not in caplog.text


def test_connection_settings_need_credentials():
    with pytest.raises(LoadError, match="DATAHARBOR_WAREHOUSE_USER, DATAHARBOR_WAREHOUSE_PASSWORD"):
        connection_settings({})

    settings = connection_settings({"DATAHARBOR_WAREHOUSE_USER": "u",
                                    "DATAHARBOR_WAREHOUSE_PASSWORD": "p"})
    assert settings == {"host": "127.0.0.1", "port": "5432", "dbname": "warehouse",
                        "user": "u", "password": "p"}


def test_postgres_load_is_idempotent_per_date(worker_client, make_sources, warehouse):
    sources, options = make_sources()
    first, last = options.business_dates[0], options.business_dates[-1]
    for day in (first, last):
        ingest(worker_client, sources, day, f"{RUN_ID}-{day}")

    load_business_date(worker_client, warehouse, sources, first)
    load_business_date(worker_client, warehouse, sources, last)
    load_business_date(worker_client, warehouse, sources, first)

    counts = dict(warehouse.execute(
        "SELECT _business_date::text, count(*) FROM raw.orders GROUP BY 1").fetchall())
    manifest = load_manifest(sources)
    assert counts == {
        str(day): entry_for(manifest, "raw_orders", day).record_count for day in (first, last)
    }
    customers = warehouse.execute("SELECT count(DISTINCT customer_id) FROM raw.customers")
    assert customers.fetchone()[0] == entry_for(manifest, "raw_customers", last).record_count
