"""Checks a fixture set against its manifest and the source contracts.

``validate`` returns one ``Finding`` per problem. A clean set returns none; a set
generated with defects returns exactly the checks listed in its manifest.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import re
from datetime import datetime
from pathlib import Path
from typing import NamedTuple

from .generator import CUSTOMER_COLUMNS, EMAIL_DOMAINS, ORDER_COLUMNS, PAYMENT_FIELDS

AMOUNT = re.compile(r"^\d+\.\d{2}$")


class Finding(NamedTuple):
    check: str
    file: str
    record: str | None = None


def read_dir(path: Path) -> dict[str, bytes]:
    """Load every file under ``path`` keyed by its POSIX relative path."""
    root = Path(path)
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()}


def _positive_amount(value: object) -> bool:
    return isinstance(value, str) and bool(AMOUNT.match(value)) and float(value) > 0


def validate(files: dict[str, bytes]) -> list[Finding]:
    manifest = json.loads(files["manifest.json"])
    findings: list[Finding] = []
    listed = {entry["path"]: entry for entry in manifest["files"]}

    for path in sorted(set(files) - set(listed) - {"manifest.json"}):
        findings.append(Finding("unlisted_file", path))

    parsed: dict[str, tuple[tuple[str, ...], list[dict]]] = {}
    for path, entry in listed.items():
        data = files.get(path)
        if data is None:
            findings.append(Finding("missing_file", path))
            continue
        if hashlib.sha256(data).hexdigest() != entry["sha256"]:
            findings.append(Finding("checksum", path))
        text = data.decode("utf-8")
        if entry["format"] == "csv":
            reader = csv.DictReader(io.StringIO(text, newline=""))
            rows = list(reader)
            header = tuple(reader.fieldnames or ())
        else:
            rows = [json.loads(line) for line in text.splitlines() if line]
            header = tuple(rows[0]) if rows else PAYMENT_FIELDS
        if len(rows) != entry["record_count"]:
            findings.append(Finding("record_count", path))
        if datetime.fromisoformat(entry["arrived_at"]) > datetime.fromisoformat(entry["expected_by"]):
            findings.append(Finding("freshness", path))
        parsed[path] = (header, rows)

    def by_source(source: str):
        for path, entry in listed.items():
            if entry["source"] == source and path in parsed:
                yield path, *parsed[path]

    customer_ids: set[str] = set()
    for path, header, rows in by_source("customers"):
        if header != CUSTOMER_COLUMNS:
            findings.append(Finding("schema", path))
            continue
        for row in rows:
            if row["customer_id"] in customer_ids:
                findings.append(Finding("unique_customer_id", path, row["customer_id"]))
            customer_ids.add(row["customer_id"])
            if row["email"].rsplit("@", 1)[-1] not in EMAIL_DOMAINS:
                findings.append(Finding("fictional_email", path, row["customer_id"]))

    order_ids: set[str] = set()
    for path, header, rows in by_source("orders"):
        schema_ok = header == ORDER_COLUMNS
        if not schema_ok:
            findings.append(Finding("schema", path))
        for row in rows:
            order_id = row.get("order_id")
            if order_id in order_ids:
                findings.append(Finding("unique_order_id", path, order_id))
            order_ids.add(order_id)
            if row.get("customer_id") not in customer_ids:
                findings.append(Finding("order_customer_exists", path, order_id))
            if schema_ok and not _positive_amount(row["amount"]):
                findings.append(Finding("amount_format", path, order_id))

    payment_ids: set[str] = set()
    for path, _header, rows in by_source("payments"):
        for row in rows:
            payment_id = row.get("payment_id")
            if tuple(row) != PAYMENT_FIELDS:
                findings.append(Finding("schema", path, payment_id))
                continue
            if payment_id in payment_ids:
                findings.append(Finding("unique_payment_id", path, payment_id))
            payment_ids.add(payment_id)
            if row["order_id"] not in order_ids:
                findings.append(Finding("payment_order_exists", path, payment_id))
            if row["amount"] is None:
                findings.append(Finding("amount_not_null", path, payment_id))
            elif not _positive_amount(row["amount"]):
                findings.append(Finding("amount_format", path, payment_id))

    return findings
