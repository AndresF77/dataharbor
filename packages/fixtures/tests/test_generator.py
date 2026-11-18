"""Acceptance tests AC-01.6 to AC-01.13 for the synthetic source data generator."""

from __future__ import annotations

import contextlib
import csv
import hashlib
import io
import json
import re
import tempfile
import unittest
from collections import defaultdict
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from dataharbor_fixtures import (
    CUSTOMER_COLUMNS,
    FIXTURE_VERSION,
    ORDER_COLUMNS,
    PAYMENT_FIELDS,
    Finding,
    Options,
    generate,
    read_dir,
    validate,
    write,
)
from dataharbor_fixtures.__main__ import main
from dataharbor_fixtures.generator import (
    DEFECTS,
    EMAIL_DOMAINS,
    FAMILY_NAMES,
    GIVEN_NAMES,
    MERCHANT_SUFFIXES,
    MERCHANT_WORDS,
    PRODUCT_NOUNS,
    PRODUCT_WORDS,
)

AMOUNT = re.compile(r"^\d+\.\d{2}$")
LONG_DIGITS = re.compile(r"\d{12,}")
SEEDS = range(0, 25)


def write_set(opts: Options, out: Path) -> dict[str, bytes]:
    write(generate(opts), out)
    return read_dir(out)


def csv_rows(data: bytes) -> tuple[list[str], list[dict]]:
    reader = csv.DictReader(io.StringIO(data.decode("utf-8"), newline=""))
    rows = list(reader)
    return list(reader.fieldnames or []), rows


def jsonl_rows(data: bytes) -> list[dict]:
    return [json.loads(line) for line in data.decode("utf-8").splitlines()]


class DeterminismTests(unittest.TestCase):
    def test_ac_01_6_same_options_write_byte_identical_files(self) -> None:
        for defects in (False, True):
            with tempfile.TemporaryDirectory() as a, tempfile.TemporaryDirectory() as b:
                first = write_set(Options(defects=defects), Path(a))
                second = write_set(Options(defects=defects), Path(b))
                self.assertEqual(first, second)

    def test_ac_01_7_different_seeds_differ_and_both_validate(self) -> None:
        one, two = generate(Options(seed=1)), generate(Options(seed=2))
        self.assertNotEqual(one.files, two.files)
        self.assertEqual(validate(one.all_files()), [])
        self.assertEqual(validate(two.all_files()), [])

    def test_clean_sets_validate_across_seeds(self) -> None:
        for seed in SEEDS:
            with self.subTest(seed=seed):
                self.assertEqual(validate(generate(Options(seed=seed)).all_files()), [])


class ShapeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.opts = Options()
        self.fx = generate(self.opts)

    def test_ac_01_8_one_file_per_business_date_and_stable_columns(self) -> None:
        days = [d.isoformat() for d in self.opts.business_dates]
        self.assertEqual(len(days), 7)
        self.assertEqual(days[-1], (self.opts.reference_date - timedelta(days=1)).isoformat())
        expected = {"customers/customers_snapshot.csv"}
        expected |= {f"orders/orders_{d}.csv" for d in days}
        expected |= {f"payments/payments_{d}.jsonl" for d in days}
        self.assertEqual(set(self.fx.files), expected)

        header, _ = csv_rows(self.fx.files["customers/customers_snapshot.csv"])
        self.assertEqual(tuple(header), CUSTOMER_COLUMNS)
        for d in days:
            header, rows = csv_rows(self.fx.files[f"orders/orders_{d}.csv"])
            self.assertEqual(tuple(header), ORDER_COLUMNS)
            self.assertTrue(rows)
            for payment in jsonl_rows(self.fx.files[f"payments/payments_{d}.jsonl"]):
                self.assertEqual(tuple(payment), PAYMENT_FIELDS)

    def test_ac_01_9_manifest_describes_every_file(self) -> None:
        m = self.fx.manifest
        self.assertEqual(m["fixture_version"], FIXTURE_VERSION)
        self.assertEqual(m["seed"], self.opts.seed)
        self.assertEqual(m["reference_date"], self.opts.reference_date.isoformat())
        self.assertEqual(
            m["business_dates"],
            {"start": self.opts.business_dates[0].isoformat(), "end": self.opts.business_dates[-1].isoformat()},
        )
        self.assertEqual({e["path"] for e in m["files"]}, set(self.fx.files))
        for entry in m["files"]:
            data = self.fx.files[entry["path"]]
            self.assertEqual(entry["sha256"], hashlib.sha256(data).hexdigest())
            if entry["format"] == "csv":
                count = len(csv_rows(data)[1])
            else:
                count = len(jsonl_rows(data))
            self.assertEqual(entry["record_count"], count)
            self.assertLessEqual(
                datetime.fromisoformat(entry["arrived_at"]), datetime.fromisoformat(entry["expected_by"])
            )

    def test_timestamps_are_utc_with_explicit_offset(self) -> None:
        for path, data in self.fx.files.items():
            if path.startswith("orders/"):
                values = [r["ordered_at"] for r in csv_rows(data)[1]]
            elif path.startswith("payments/"):
                values = [r["paid_at"] for r in jsonl_rows(data)]
            else:
                continue
            day = path.rsplit("_", 1)[1].split(".")[0]
            for value in values:
                self.assertTrue(value.endswith("+00:00"), value)
                self.assertEqual(datetime.fromisoformat(value).date().isoformat(), day)


class IntegrityTests(unittest.TestCase):
    def test_ac_01_10_clean_set_is_referentially_sound(self) -> None:
        fx = generate(Options())
        customers = {r["customer_id"] for r in csv_rows(fx.files["customers/customers_snapshot.csv"])[1]}
        orders: dict[str, dict] = {}
        payments: list[dict] = []
        for path, data in fx.files.items():
            if path.startswith("orders/"):
                for row in csv_rows(data)[1]:
                    self.assertNotIn(row["order_id"], orders)
                    orders[row["order_id"]] = row
            elif path.startswith("payments/"):
                payments.extend(jsonl_rows(data))

        for order in orders.values():
            self.assertIn(order["customer_id"], customers)
            self.assertRegex(order["amount"], AMOUNT)
            self.assertGreater(float(order["amount"]), 0)
            self.assertEqual(
                Decimal(order["amount"]), Decimal(order["unit_price"]) * int(order["quantity"])
            )
        paid = defaultdict(int)
        for payment in payments:
            order = orders[payment["order_id"]]
            self.assertEqual(payment["amount"], order["amount"])
            self.assertEqual(order["status"], "completed")
            paid[payment["order_id"]] += 1
        completed = {o for o, row in orders.items() if row["status"] == "completed"}
        self.assertEqual(set(paid), completed)
        self.assertEqual(set(paid.values()), {1})

    def test_ac_01_11_names_and_contacts_are_plainly_fictional(self) -> None:
        for seed in SEEDS:
            fx = generate(Options(seed=seed, defects=True))
            for row in csv_rows(fx.files["customers/customers_snapshot.csv"])[1]:
                given, family = row["customer_name"].split(" ")
                self.assertIn(given, GIVEN_NAMES)
                self.assertIn(family, FAMILY_NAMES)
                self.assertIn(row["email"].rsplit("@", 1)[1], EMAIL_DOMAINS)
            for path, data in fx.files.items():
                self.assertIsNone(LONG_DIGITS.search(data.decode("utf-8")), path)
                if path.startswith("orders/"):
                    for row in csv_rows(data)[1]:
                        merchant_word, merchant_suffix = row["merchant"].split(" ")
                        product_word, product_noun = row["product"].split(" ")
                        self.assertIn(merchant_word, MERCHANT_WORDS)
                        self.assertIn(merchant_suffix, MERCHANT_SUFFIXES)
                        self.assertIn(product_word, PRODUCT_WORDS)
                        self.assertIn(product_noun, PRODUCT_NOUNS)


class DefectTests(unittest.TestCase):
    def test_ac_01_12_every_defect_is_listed_and_caught_by_its_check(self) -> None:
        for seed in SEEDS:
            with self.subTest(seed=seed):
                fx = generate(Options(seed=seed, defects=True))
                defects = fx.manifest["defects"]
                self.assertEqual({d["type"] for d in defects}, set(DEFECTS))
                self.assertEqual(len({d["file"] for d in defects}), len(defects))

                expected = set()
                for d in defects:
                    self.assertTrue(d["records"])
                    if d["scope"] == "file":
                        expected.add(Finding(d["check"], d["file"]))
                    else:
                        expected |= {Finding(d["check"], d["file"], r) for r in d["records"]}
                self.assertEqual(set(validate(fx.all_files())), expected)

    def test_renamed_column_and_late_file_details(self) -> None:
        fx = generate(Options(defects=True))
        by_type = {d["type"]: d for d in fx.manifest["defects"]}
        renamed = by_type["renamed_column"]
        header, _ = csv_rows(fx.files[renamed["file"]])
        self.assertNotIn(renamed["column"], header)
        self.assertIn(renamed["renamed_to"], header)

        late = by_type["late_file"]
        entry = next(e for e in fx.manifest["files"] if e["path"] == late["file"])
        self.assertEqual(entry["business_date"], fx.manifest["business_dates"]["end"])
        self.assertGreater(
            datetime.fromisoformat(entry["arrived_at"]) - datetime.fromisoformat(entry["expected_by"]),
            timedelta(hours=24),
        )

    def test_ac_01_13_defects_touch_only_listed_files(self) -> None:
        for seed in SEEDS:
            with self.subTest(seed=seed):
                clean = generate(Options(seed=seed))
                dirty = generate(Options(seed=seed, defects=True))
                self.assertEqual(set(clean.files), set(dirty.files))
                listed = {d["file"] for d in dirty.manifest["defects"]}
                changed = {p for p in clean.files if clean.files[p] != dirty.files[p]}
                content_defects = {d["file"] for d in dirty.manifest["defects"] if d["type"] != "late_file"}
                self.assertLessEqual(changed, listed)
                self.assertEqual(changed, content_defects)

                clean_entries = {e["path"]: e for e in clean.manifest["files"]}
                for entry in dirty.manifest["files"]:
                    if entry["path"] not in listed:
                        self.assertEqual(entry, clean_entries[entry["path"]])


class OptionTests(unittest.TestCase):
    def test_defects_need_enough_business_dates(self) -> None:
        with self.assertRaises(ValueError):
            Options(days=5, defects=True)
        self.assertEqual(validate(generate(Options(days=1)).all_files()), [])

    def test_days_must_be_in_range(self) -> None:
        for days in (0, -3, 367):
            with self.assertRaises(ValueError):
                Options(days=days)

    def test_write_refuses_a_non_empty_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "stale.csv").write_text("x\n")
            with self.assertRaises(FileExistsError):
                write(generate(), Path(tmp))

    def test_checker_reports_tampering(self) -> None:
        files = generate().all_files()
        path = "orders/orders_2026-02-25.csv"
        files[path] = files[path].replace(b",USD,", b",EUR,", 1)
        files["orders/extra.csv"] = b"order_id\n"
        findings = validate(files)
        self.assertIn(Finding("checksum", path), findings)
        self.assertIn(Finding("unlisted_file", "orders/extra.csv"), findings)


def run_cli(*args: str) -> int:
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        return main(list(args))


class CliTests(unittest.TestCase):
    def test_generate_then_check(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            clean, dirty = Path(tmp) / "clean", Path(tmp) / "dirty"
            self.assertEqual(run_cli("generate", "--out", str(clean), "--seed", "7"), 0)
            self.assertEqual(run_cli("generate", "--out", str(dirty), "--seed", "7", "--defects"), 0)
            self.assertEqual(json.loads((clean / "manifest.json").read_text())["seed"], 7)
            self.assertEqual(run_cli("check", str(clean)), 0)
            self.assertEqual(run_cli("check", str(dirty)), 1)

    def test_reference_date_option(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_cli("generate", "--out", tmp, "--reference-date", "2026-01-10", "--days", "2")
            manifest = json.loads((Path(tmp) / "manifest.json").read_text())
            self.assertEqual(manifest["business_dates"], {"start": "2026-01-08", "end": "2026-01-09"})
            self.assertEqual(date.fromisoformat(manifest["reference_date"]), date(2026, 1, 10))


if __name__ == "__main__":
    unittest.main()
