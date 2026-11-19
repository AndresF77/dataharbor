"""Ingestion contract shape and the checks that run a source file against it.

A contract describes what a raw source file must look like: its format
(`csv` or `jsonl`), its columns with a type and whether they may be null,
and the primary-key columns that must be unique within one file.

`validate_content` is pure (no database, no I/O) so the API, the ingestion
DAG and tests share one implementation. Violations carry a rule, the
column and the 1-based record number, never the offending value, so
reports and logs cannot leak source data.
"""

import csv
import enum
import io
import json
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

COLUMN_NAME_PATTERN = r"^[a-z][a-z0-9_]{0,62}$"
MAX_COLUMNS = 200
MAX_KEY_COLUMNS = 8
MAX_REPORTED_VIOLATIONS = 100


class SourceFormat(str, enum.Enum):
    CSV = "csv"
    JSONL = "jsonl"


class ColumnType(str, enum.Enum):
    STRING = "string"
    INTEGER = "integer"
    DECIMAL = "decimal"
    DATE = "date"
    TIMESTAMP = "timestamp"
    BOOLEAN = "boolean"


class ContractColumn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(pattern=COLUMN_NAME_PATTERN)
    type: ColumnType
    nullable: bool = False


class ContractSpec(BaseModel):
    """The columns and key a source file must match."""

    model_config = ConfigDict(extra="forbid")

    format: SourceFormat
    columns: list[ContractColumn] = Field(min_length=1, max_length=MAX_COLUMNS)
    primary_key: list[str] = Field(default_factory=list, max_length=MAX_KEY_COLUMNS)

    @model_validator(mode="after")
    def _check_columns_and_key(self) -> "ContractSpec":
        names = [c.name for c in self.columns]
        duplicates = sorted({n for n in names if names.count(n) > 1})
        if duplicates:
            raise ValueError(f"duplicate column names: {', '.join(duplicates)}")
        if len(set(self.primary_key)) != len(self.primary_key):
            raise ValueError("primary_key lists a column more than once")
        by_name = {c.name: c for c in self.columns}
        for key in self.primary_key:
            if key not in by_name:
                raise ValueError(f"primary_key column {key!r} is not in columns")
            if by_name[key].nullable:
                raise ValueError(f"primary_key column {key!r} cannot be nullable")
        return self


class Rule(str, enum.Enum):
    ENCODING = "encoding"
    EMPTY_FILE = "empty_file"
    DUPLICATE_COLUMN = "duplicate_column"
    MISSING_COLUMN = "missing_column"
    UNEXPECTED_COLUMN = "unexpected_column"
    MALFORMED_RECORD = "malformed_record"
    NULL_VALUE = "null_value"
    TYPE_MISMATCH = "type_mismatch"
    DUPLICATE_KEY = "duplicate_key"


@dataclass(frozen=True)
class Violation:
    rule: Rule
    message: str
    column: str | None = None
    record: int | None = None  # 1-based; None for file-level problems


@dataclass
class ValidationReport:
    record_count: int = 0
    violation_count: int = 0
    violations: list[Violation] = field(default_factory=list)

    @property
    def valid(self) -> bool:
        return self.violation_count == 0

    @property
    def truncated(self) -> bool:
        return self.violation_count > len(self.violations)

    def add(self, violation: Violation) -> None:
        self.violation_count += 1
        if len(self.violations) < MAX_REPORTED_VIOLATIONS:
            self.violations.append(violation)


_INTEGER = re.compile(r"^[+-]?\d+$")
_DECIMAL = re.compile(r"^[+-]?(\d+(\.\d*)?|\.\d+)$")
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_TIMESTAMP = re.compile(r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(:\d{2}(\.\d{1,6})?)?(Z|[+-]\d{2}:\d{2})$")


def _matches_type(column_type: ColumnType, value: Any) -> bool:
    """True if a non-null value fits the type. JSON natives and their text forms both count."""
    if column_type is ColumnType.STRING:
        return isinstance(value, str)
    if isinstance(value, bool):
        return column_type is ColumnType.BOOLEAN
    if column_type is ColumnType.INTEGER:
        return isinstance(value, int) or (isinstance(value, str) and bool(_INTEGER.match(value)))
    if column_type is ColumnType.DECIMAL:
        if isinstance(value, int | float):
            return value == value and value not in (float("inf"), float("-inf"))
        if not (isinstance(value, str) and _DECIMAL.match(value)):
            return False
        try:
            Decimal(value)
        except InvalidOperation:
            return False
        return True
    if not isinstance(value, str):
        return False
    if column_type is ColumnType.BOOLEAN:
        return value.lower() in ("true", "false")
    if column_type is ColumnType.DATE:
        if not _DATE.match(value):
            return False
        try:
            date.fromisoformat(value)
        except ValueError:
            return False
        return True
    if column_type is ColumnType.TIMESTAMP:
        if not _TIMESTAMP.match(value):
            return False  # also rejects timestamps without a UTC offset
        try:
            datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return False
        return True
    return False


def _is_null(value: Any) -> bool:
    return value is None or value == ""


class _RecordChecker:
    def __init__(self, spec: ContractSpec, report: ValidationReport) -> None:
        self.spec = spec
        self.report = report
        self.columns = {c.name: c for c in spec.columns}
        self.seen_keys: dict[tuple, int] = {}

    def check(self, record_number: int, values: dict[str, Any]) -> None:
        for name, value in values.items():
            column = self.columns.get(name)
            if column is None:
                continue
            if _is_null(value):
                if not column.nullable:
                    self.report.add(
                        Violation(Rule.NULL_VALUE, f"{name} must not be empty", name, record_number)
                    )
            elif not _matches_type(column.type, value):
                self.report.add(
                    Violation(
                        Rule.TYPE_MISMATCH,
                        f"{name} is not a valid {column.type.value}",
                        name,
                        record_number,
                    )
                )
        if not self.spec.primary_key:
            return
        key_values = [values.get(k) for k in self.spec.primary_key]
        if any(_is_null(v) for v in key_values):
            return  # already reported as missing or null
        key = tuple(str(v) for v in key_values)
        first = self.seen_keys.setdefault(key, record_number)
        if first != record_number:
            self.report.add(
                Violation(
                    Rule.DUPLICATE_KEY,
                    f"{', '.join(self.spec.primary_key)} repeats record {first}",
                    ",".join(self.spec.primary_key),
                    record_number,
                )
            )


def _decode(data: bytes, report: ValidationReport) -> str | None:
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        report.add(Violation(Rule.ENCODING, "File is not valid UTF-8"))
        return None


def _validate_csv(spec: ContractSpec, text: str, report: ValidationReport) -> None:
    rows = csv.reader(io.StringIO(text, newline=""), strict=True)
    try:
        header = next((row for row in rows if row), None)
        if header is None:
            report.add(Violation(Rule.EMPTY_FILE, "File has no header row"))
            return
        header = [h.strip() for h in header]
        for name in sorted({h for h in header if header.count(h) > 1}):
            report.add(Violation(Rule.DUPLICATE_COLUMN, f"{name} appears more than once", name))
        expected = [c.name for c in spec.columns]
        for name in expected:
            if name not in header:
                report.add(Violation(Rule.MISSING_COLUMN, f"{name} is missing", name))
        for name in header:
            if name not in expected:
                report.add(Violation(Rule.UNEXPECTED_COLUMN, f"{name} is not in the contract", name))

        checker = _RecordChecker(spec, report)
        for row in rows:
            if not row:
                continue  # blank line
            report.record_count += 1
            if len(row) != len(header):
                report.add(
                    Violation(
                        Rule.MALFORMED_RECORD,
                        f"Expected {len(header)} fields, found {len(row)}",
                        record=report.record_count,
                    )
                )
                continue
            checker.check(report.record_count, dict(zip(header, row, strict=True)))
    except csv.Error:
        report.add(
            Violation(Rule.MALFORMED_RECORD, "CSV could not be parsed", record=report.record_count + 1)
        )


def _validate_jsonl(spec: ContractSpec, text: str, report: ValidationReport) -> None:
    expected = [c.name for c in spec.columns]
    checker = _RecordChecker(spec, report)
    for line in text.splitlines():
        if not line.strip():
            continue
        report.record_count += 1
        number = report.record_count
        try:
            record = json.loads(line)
        except ValueError:
            record = None
        if not isinstance(record, dict):
            report.add(Violation(Rule.MALFORMED_RECORD, "Line is not a JSON object", record=number))
            continue
        for name in expected:
            if name not in record:
                report.add(Violation(Rule.MISSING_COLUMN, f"{name} is missing", name, number))
        for name in record:
            if name not in expected:
                report.add(
                    Violation(Rule.UNEXPECTED_COLUMN, f"{name} is not in the contract", name, number)
                )
        checker.check(number, record)
    if report.record_count == 0:
        report.add(Violation(Rule.EMPTY_FILE, "File has no records"))


def validate_content(spec: ContractSpec, data: bytes) -> ValidationReport:
    """Check one source file against a contract and report every violation found."""
    report = ValidationReport()
    text = _decode(data, report)
    if text is None:
        return report
    if spec.format is SourceFormat.CSV:
        _validate_csv(spec, text, report)
    else:
        _validate_jsonl(spec, text, report)
    return report
