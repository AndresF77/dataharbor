import json

import pytest
from pydantic import ValidationError

from dataharbor_api.contract_rules import (
    MAX_REPORTED_VIOLATIONS,
    ColumnType,
    ContractSpec,
    Rule,
    _matches_type,
    validate_content,
)

ORDERS = ContractSpec.model_validate(
    {
        "format": "csv",
        "columns": [
            {"name": "order_id", "type": "string"},
            {"name": "quantity", "type": "integer"},
            {"name": "amount", "type": "decimal"},
            {"name": "ordered_at", "type": "timestamp"},
            {"name": "note", "type": "string", "nullable": True},
        ],
        "primary_key": ["order_id"],
    }
)
PAYMENTS = ContractSpec.model_validate(
    {
        "format": "jsonl",
        "columns": [
            {"name": "payment_id", "type": "string"},
            {"name": "amount", "type": "decimal"},
            {"name": "captured", "type": "boolean"},
            {"name": "paid_on", "type": "date"},
        ],
        "primary_key": ["payment_id"],
    }
)


def csv_bytes(*lines: str) -> bytes:
    return ("\n".join(lines) + "\n").encode()


def jsonl_bytes(*records: dict) -> bytes:
    return "".join(json.dumps(r) + "\n" for r in records).encode()


def rules(report) -> list[tuple[str, str | None, int | None]]:
    return [(v.rule.value, v.column, v.record) for v in report.violations]


def test_clean_csv_is_valid():
    report = validate_content(
        ORDERS,
        csv_bytes(
            "order_id,quantity,amount,ordered_at,note",
            "ORD-1,2,19.90,2026-03-01T10:15:00+00:00,",
            "ORD-2,1,5,2026-03-01T11:00:00Z,gift",
        ),
    )
    assert report.valid
    assert report.record_count == 2
    assert report.violations == []


def test_csv_column_order_does_not_matter():
    report = validate_content(
        ORDERS,
        csv_bytes("note,ordered_at,amount,quantity,order_id", ",2026-03-01T10:15:00Z,1.00,1,ORD-1"),
    )
    assert report.valid


def test_csv_renamed_column_is_missing_and_unexpected():
    report = validate_content(
        ORDERS,
        csv_bytes(
            "order_id,quantity,order_amount,ordered_at,note",
            "ORD-1,2,19.90,2026-03-01T10:15:00Z,",
        ),
    )
    assert rules(report) == [
        ("missing_column", "amount", None),
        ("unexpected_column", "order_amount", None),
    ]


def test_csv_duplicate_header():
    report = validate_content(
        ORDERS,
        csv_bytes("order_id,quantity,amount,amount,ordered_at,note", "ORD-1,1,1,1,2026-03-01T10:15:00Z,"),
    )
    assert ("duplicate_column", "amount", None) in rules(report)


def test_csv_nulls_types_and_duplicate_keys():
    report = validate_content(
        ORDERS,
        csv_bytes(
            "order_id,quantity,amount,ordered_at,note",
            "ORD-1,2,19.90,2026-03-01T10:15:00Z,",
            "ORD-2,two,,2026-03-01 10:15:00,",
            "ORD-1,1,1.5,2026-03-01T12:00:00Z,",
            ",1,1.5,2026-03-01T12:00:00Z,",
        ),
    )
    assert rules(report) == [
        ("type_mismatch", "quantity", 2),
        ("null_value", "amount", 2),
        ("type_mismatch", "ordered_at", 2),  # no UTC offset
        ("duplicate_key", "order_id", 3),
        ("null_value", "order_id", 4),
    ]
    assert report.violations[3].message == "order_id repeats record 1"


def test_csv_wrong_field_count_is_malformed():
    report = validate_content(
        ORDERS,
        csv_bytes("order_id,quantity,amount,ordered_at,note", "ORD-1,2,19.90"),
    )
    assert rules(report) == [("malformed_record", None, 1)]


def test_csv_without_header_is_empty():
    assert rules(validate_content(ORDERS, b"\n")) == [("empty_file", None, None)]


def test_csv_with_bom_and_blank_lines():
    data = "﻿order_id,quantity,amount,ordered_at,note\n\nORD-1,1,1,2026-03-01T10:00:00Z,\n\n"
    report = validate_content(ORDERS, data.encode())
    assert report.valid
    assert report.record_count == 1


def test_non_utf8_file():
    report = validate_content(ORDERS, "order_id\nÖ\n".encode("latin-1"))
    assert rules(report) == [("encoding", None, None)]


def test_clean_jsonl_accepts_native_and_text_values():
    report = validate_content(
        PAYMENTS,
        jsonl_bytes(
            {"payment_id": "PAY-1", "amount": "12.50", "captured": True, "paid_on": "2026-03-01"},
            {"payment_id": "PAY-2", "amount": 7, "captured": "false", "paid_on": "2026-03-02"},
        ),
    )
    assert report.valid
    assert report.record_count == 2


def test_jsonl_record_problems():
    report = validate_content(
        PAYMENTS,
        jsonl_bytes(
            {"payment_id": "PAY-1", "amount": None, "captured": True, "paid_on": "2026-02-30"},
            {"payment_id": "PAY-1", "amount": "1", "captured": 1, "paid_on": "2026-03-01", "x": 1},
            {"payment_id": "PAY-3", "captured": True, "paid_on": "2026-03-01"},
        )
        + b"[1, 2]\nnot json\n",
    )
    assert rules(report) == [
        ("null_value", "amount", 1),
        ("type_mismatch", "paid_on", 1),
        ("unexpected_column", "x", 2),
        ("type_mismatch", "captured", 2),
        ("duplicate_key", "payment_id", 2),
        ("missing_column", "amount", 3),
        ("malformed_record", None, 4),
        ("malformed_record", None, 5),
    ]


def test_empty_jsonl():
    assert rules(validate_content(PAYMENTS, b"\n\n")) == [("empty_file", None, None)]


def test_violations_never_include_values():
    secret = "ada@example.test"
    report = validate_content(
        ORDERS,
        csv_bytes("order_id,quantity,amount,ordered_at,note", f"ORD-1,{secret},1,2026-03-01T10:00:00Z,"),
    )
    assert not report.valid
    assert all(secret not in v.message for v in report.violations)


def test_reported_violations_are_capped():
    lines = ["order_id,quantity,amount,ordered_at,note"]
    lines += [f"ORD-{i},x,1,2026-03-01T10:00:00Z," for i in range(MAX_REPORTED_VIOLATIONS + 20)]
    report = validate_content(ORDERS, csv_bytes(*lines))
    assert report.violation_count == MAX_REPORTED_VIOLATIONS + 20
    assert len(report.violations) == MAX_REPORTED_VIOLATIONS
    assert report.truncated


@pytest.mark.parametrize(
    ("column_type", "good", "bad"),
    [
        (ColumnType.INTEGER, ["0", "-12", 42], ["1.5", "1e3", 1.0, True, ""]),
        (ColumnType.DECIMAL, ["19.90", "-3", ".5", 2.5, 3], ["1,5", "NaN", float("nan"), False]),
        (ColumnType.DATE, ["2024-02-29"], ["2023-02-29", "2026-3-1", "2026-03-01T00:00:00Z"]),
        (
            ColumnType.TIMESTAMP,
            ["2026-03-01T10:15:00+00:00", "2026-03-01T10:15:00.123456Z", "2026-03-01 10:15-05:00"],
            ["2026-03-01T10:15:00", "2026-03-01", "2026-13-01T10:15:00Z"],
        ),
        (ColumnType.BOOLEAN, ["true", "FALSE", True], ["yes", "1", 1]),
        (ColumnType.STRING, ["", "anything"], [1, True, {"a": 1}]),
    ],
)
def test_type_rules(column_type, good, bad):
    assert all(_matches_type(column_type, v) for v in good)
    assert not any(_matches_type(column_type, v) for v in bad)


@pytest.mark.parametrize(
    ("spec", "message"),
    [
        ({"format": "csv", "columns": []}, "at least 1"),
        (
            {"format": "csv", "columns": [{"name": "a", "type": "string"}] * 2},
            "duplicate column names: a",
        ),
        (
            {"format": "csv", "columns": [{"name": "a", "type": "string"}], "primary_key": ["b"]},
            "'b' is not in columns",
        ),
        (
            {
                "format": "csv",
                "columns": [{"name": "a", "type": "string", "nullable": True}],
                "primary_key": ["a"],
            },
            "cannot be nullable",
        ),
        ({"format": "parquet", "columns": [{"name": "a", "type": "string"}]}, "format"),
        ({"format": "csv", "columns": [{"name": "Bad Name", "type": "string"}]}, "pattern"),
    ],
)
def test_invalid_specs(spec, message):
    with pytest.raises(ValidationError, match=message):
        ContractSpec.model_validate(spec)
