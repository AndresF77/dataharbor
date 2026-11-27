"""Data quality checks and the engine that runs them over a landed file.

A landed file already matches its ingestion contract (columns, types,
nullability, primary key). Quality checks go further and test the values:

| check | shape | fails for |
|---|---|---|
| `not_null` | one column | each record where the column is null or empty |
| `unique` | 1–8 columns | each record repeating an earlier record's values (nulls skipped) |
| `accepted_values` | one column, `values` | each record whose value is not listed (nulls skipped) |
| `value_range` | one integer/decimal/date/timestamp column, `min` and/or `max` | each record outside the inclusive range (nulls skipped) |
| `row_count` | no column, `min` and/or `max` | the file, when its record count is outside the inclusive range |

`run_checks` is pure (no database, no I/O) and reads the file once for all
checks. Outcomes carry counts and 1-based record numbers, never values, so
results and logs cannot leak source data.
"""

import csv
import enum
import io
import json
import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from dataharbor_api.contract_rules import (
    COLUMN_NAME_PATTERN,
    MAX_KEY_COLUMNS,
    ColumnType,
    ContractSpec,
    SourceFormat,
)

MAX_ACCEPTED_VALUES = 100
MAX_VALUE_LENGTH = 200
MAX_SAMPLE_RECORDS = 20
RANGE_TYPES = (ColumnType.INTEGER, ColumnType.DECIMAL, ColumnType.DATE, ColumnType.TIMESTAMP)


class CheckKind(str, enum.Enum):
    NOT_NULL = "not_null"
    UNIQUE = "unique"
    ACCEPTED_VALUES = "accepted_values"
    VALUE_RANGE = "value_range"
    ROW_COUNT = "row_count"


SINGLE_COLUMN = (CheckKind.NOT_NULL, CheckKind.ACCEPTED_VALUES, CheckKind.VALUE_RANGE)
BOUNDED = (CheckKind.VALUE_RANGE, CheckKind.ROW_COUNT)


class CheckSpec(BaseModel):
    """What one quality rule checks. Bounds are strings so decimals and dates stay exact."""

    model_config = ConfigDict(extra="forbid")

    check: CheckKind
    columns: list[str] = Field(default_factory=list, max_length=MAX_KEY_COLUMNS)
    values: list[str] | None = Field(default=None, max_length=MAX_ACCEPTED_VALUES)
    min: str | None = Field(default=None, max_length=40)
    max: str | None = Field(default=None, max_length=40)

    @model_validator(mode="after")
    def _shape_matches_check(self) -> "CheckSpec":
        check = self.check
        for name in self.columns:
            if not _is_column_name(name):
                raise ValueError(f"{name!r} is not a valid column name")
        if len(set(self.columns)) != len(self.columns):
            raise ValueError("columns lists a column more than once")
        if check in SINGLE_COLUMN and len(self.columns) != 1:
            raise ValueError(f"{check.value} checks exactly one column")
        if check is CheckKind.UNIQUE and not self.columns:
            raise ValueError("unique needs at least one column")
        if check is CheckKind.ROW_COUNT and self.columns:
            raise ValueError("row_count does not take columns")

        if check is CheckKind.ACCEPTED_VALUES:
            if not self.values:
                raise ValueError("accepted_values needs at least one value")
            if any(len(v) > MAX_VALUE_LENGTH for v in self.values):
                raise ValueError(f"accepted values are at most {MAX_VALUE_LENGTH} characters")
            if len(set(self.values)) != len(self.values):
                raise ValueError("values lists a value more than once")
        elif self.values is not None:
            raise ValueError("values is only allowed with accepted_values")

        if check in BOUNDED:
            if self.min is None and self.max is None:
                raise ValueError(f"{check.value} needs min, max or both")
            if check is CheckKind.ROW_COUNT:
                low, high = (_count_bound(b) for b in (self.min, self.max))
                if low is not None and high is not None and low > high:
                    raise ValueError("min must not be above max")
        elif self.min is not None or self.max is not None:
            raise ValueError("min and max are only allowed with value_range and row_count")
        return self

    def problems_with(self, contract: ContractSpec) -> list[str]:
        """Why this check cannot run on files of `contract` (empty when it can)."""
        types = {c.name: c.type for c in contract.columns}
        problems = [f"{name} is not a column of the contract" for name in self.columns
                    if name not in types]
        if problems or self.check is not CheckKind.VALUE_RANGE:
            return problems
        column_type = types[self.columns[0]]
        if column_type not in RANGE_TYPES:
            return [f"value_range needs an integer, decimal, date or timestamp column, "
                    f"{self.columns[0]} is {column_type.value}"]
        bounds = []
        for label, raw in (("min", self.min), ("max", self.max)):
            if raw is None:
                bounds.append(None)
                continue
            parsed = parse_typed(column_type, raw)
            if parsed is None:
                return [f"{label} is not a valid {column_type.value}"]
            bounds.append(parsed)
        if bounds[0] is not None and bounds[1] is not None and bounds[0] > bounds[1]:
            return ["min must not be above max"]
        return []


def _is_column_name(name: str) -> bool:
    return re.fullmatch(COLUMN_NAME_PATTERN, name) is not None


def _count_bound(raw: str | None) -> int | None:
    if raw is None:
        return None
    if not raw.isdigit():
        raise ValueError("row_count bounds must be whole numbers ≥ 0")
    return int(raw)


def parse_typed(column_type: ColumnType, value: Any) -> Any:
    """A comparable value for range checks, or None if `value` does not fit the type."""
    if isinstance(value, bool):
        return None
    text = value if isinstance(value, str) else str(value)
    try:
        if column_type is ColumnType.INTEGER:
            return int(value) if isinstance(value, int) else int(text.strip(), 10)
        if column_type is ColumnType.DECIMAL:
            parsed = Decimal(text.strip())
            return parsed if parsed.is_finite() else None
        if column_type is ColumnType.DATE:
            return date.fromisoformat(text)
        if column_type is ColumnType.TIMESTAMP:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
            return parsed if parsed.tzinfo is not None else None
    except (ValueError, InvalidOperation):
        return None
    return None


class QualityInputError(Exception):
    """The stored file could not be read as records (it should have passed its contract)."""


@dataclass
class CheckOutcome:
    passed: bool
    checked_records: int = 0
    failing_records: int = 0
    sample_records: list[int] = field(default_factory=list)
    message: str = ""

    def fail(self, record: int) -> None:
        self.failing_records += 1
        if len(self.sample_records) < MAX_SAMPLE_RECORDS:
            self.sample_records.append(record)


def _is_null(value: Any) -> bool:
    return value is None or value == ""


def _text(value: Any) -> str:
    """How a value compares with accepted values: JSON booleans as `true`/`false`."""
    if isinstance(value, bool):
        return "true" if value else "false"
    return value if isinstance(value, str) else json.dumps(value)


def iter_records(source_format: SourceFormat, data: bytes) -> Iterator[dict[str, Any]]:
    """Records in file order, numbered like contract validation (blank lines skipped)."""
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise QualityInputError("File is not valid UTF-8") from None
    if source_format is SourceFormat.CSV:
        rows = csv.reader(io.StringIO(text, newline=""), strict=True)
        try:
            header = next((row for row in rows if row), None)
            if header is None:
                return
            header = [h.strip() for h in header]
            for row in rows:
                if not row:
                    continue
                if len(row) != len(header):
                    raise QualityInputError("A record does not match the header")
                yield dict(zip(header, row, strict=True))
        except csv.Error:
            raise QualityInputError("CSV could not be parsed") from None
        return
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except ValueError:
            record = None
        if not isinstance(record, dict):
            raise QualityInputError("A line is not a JSON object")
        yield record


class _Runner:
    def __init__(self, spec: CheckSpec, contract: ContractSpec) -> None:
        self.spec = spec
        self.outcome = CheckOutcome(passed=True)
        self.column = spec.columns[0] if spec.columns else None
        self.seen: set[tuple[str, ...]] = set()
        self.accepted = set(spec.values or [])
        self.low = self.high = None
        if spec.check is CheckKind.VALUE_RANGE:
            self.column_type = next(c.type for c in contract.columns if c.name == self.column)
            self.low = parse_typed(self.column_type, spec.min) if spec.min is not None else None
            self.high = parse_typed(self.column_type, spec.max) if spec.max is not None else None

    def record(self, number: int, values: dict[str, Any]) -> None:
        check, outcome = self.spec.check, self.outcome
        if check is CheckKind.ROW_COUNT:
            return
        if check is CheckKind.UNIQUE:
            key_values = [values.get(c) for c in self.spec.columns]
            if any(_is_null(v) for v in key_values):
                return
            outcome.checked_records += 1
            key = tuple(_text(v) for v in key_values)
            if key in self.seen:
                outcome.fail(number)
            else:
                self.seen.add(key)
            return
        value = values.get(self.column)
        if check is CheckKind.NOT_NULL:
            outcome.checked_records += 1
            if _is_null(value):
                outcome.fail(number)
            return
        if _is_null(value):
            return  # not_null decides about missing values
        outcome.checked_records += 1
        if check is CheckKind.ACCEPTED_VALUES:
            if _text(value) not in self.accepted:
                outcome.fail(number)
            return
        parsed = parse_typed(self.column_type, value)
        if parsed is None or (self.low is not None and parsed < self.low) or (
            self.high is not None and parsed > self.high
        ):
            outcome.fail(number)

    def finish(self, record_count: int) -> CheckOutcome:
        outcome, spec = self.outcome, self.spec
        if spec.check is CheckKind.ROW_COUNT:
            outcome.checked_records = record_count
            low = int(spec.min) if spec.min is not None else None
            high = int(spec.max) if spec.max is not None else None
            outcome.passed = (low is None or record_count >= low) and (
                high is None or record_count <= high
            )
            expected = " and ".join(
                part for part in (
                    f"at least {low}" if low is not None else "",
                    f"at most {high}" if high is not None else "",
                ) if part
            )
            outcome.message = f"{record_count} records; expected {expected}"
            return outcome
        outcome.passed = outcome.failing_records == 0
        noun = "record" if outcome.failing_records == 1 else "records"
        outcome.message = (
            f"{outcome.failing_records} of {outcome.checked_records} checked {noun} failed"
            if not outcome.passed
            else f"{outcome.checked_records} records checked"
        )
        return outcome


def run_checks(
    contract: ContractSpec, data: bytes, checks: list[CheckSpec]
) -> tuple[int, list[CheckOutcome]]:
    """Run every check over one file in a single pass; return (record count, outcomes).

    Each check must have no `problems_with(contract)`. Raises
    `QualityInputError` if the file cannot be read as records.
    """
    runners = [_Runner(spec, contract) for spec in checks]
    record_count = 0
    for record_count, record in enumerate(iter_records(contract.format, data), start=1):
        for runner in runners:
            runner.record(record_count, record)
    return record_count, [runner.finish(record_count) for runner in runners]
