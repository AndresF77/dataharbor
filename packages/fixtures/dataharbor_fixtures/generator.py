"""Generate deterministic synthetic orders, payments and customers.

The same options always produce byte-identical files. Business dates are the
``days`` UTC dates before the reference date. Defects for the quality-check demo
are applied on top of the clean set, so files they do not touch are unchanged.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import random
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from pathlib import Path

FIXTURE_VERSION = "1.0.0"
DEFAULT_SEED = 1042
DEFAULT_REFERENCE_DATE = date(2026, 3, 2)
DEFAULT_DAYS = 7
MAX_DAYS = 366
MIN_DAYS_WITH_DEFECTS = 6

CUSTOMER_COLUMNS = ("customer_id", "customer_name", "email", "country", "segment", "signup_date")
ORDER_COLUMNS = (
    "order_id",
    "customer_id",
    "merchant",
    "product",
    "quantity",
    "unit_price",
    "amount",
    "currency",
    "status",
    "ordered_at",
)
PAYMENT_FIELDS = ("payment_id", "order_id", "method", "amount", "currency", "status", "paid_at")

CUSTOMERS_PATH = "customers/customers_snapshot.csv"

# Word lists: every generated name is built only from these.
GIVEN_NAMES = (
    "Arlo", "Bexley", "Calla", "Dorian", "Elowen", "Fenna", "Gideon", "Halia",
    "Ivo", "Juno", "Kestrel", "Linnea", "Marlo", "Nyra", "Orrin", "Perrin",
    "Quilla", "Rowan", "Sable", "Tamsin",
)
FAMILY_NAMES = (
    "Ashgrove", "Brindlewood", "Copperfield", "Dunmere", "Emberly", "Fallowmere",
    "Glimmerdale", "Hollowell", "Inkwater", "Juniperry", "Kettleby", "Larkspur",
    "Mossbank", "Nettlefold", "Oakhurst", "Quillfeather",
)
MERCHANT_WORDS = ("Lantern", "Harbor", "Copper", "Juniper", "Meadow", "Saffron", "Granite", "Willow")
MERCHANT_SUFFIXES = ("Goods", "Supply", "Trading", "Market", "Works", "Outfitters")
PRODUCT_WORDS = ("Granite", "Linen", "Cedar", "Cobalt", "Amber", "Fern", "Slate", "Clover")
PRODUCT_NOUNS = ("Mug", "Notebook", "Lamp", "Backpack", "Kettle", "Scarf", "Planter", "Tote")
EMAIL_DOMAINS = ("example.com", "example.org")
COUNTRIES = ("AU", "CA", "CO", "DE", "ES", "GB", "MX", "US")
SEGMENTS = ("consumer", "small_business")
PAYMENT_METHODS = ("card", "bank_transfer", "wallet")
CURRENCY = "USD"

CUSTOMER_COUNT = 40
MERCHANT_COUNT = 6
PRODUCT_COUNT = 12
ORDERS_PER_DAY = (18, 30)
CANCEL_RATE = 0.08
ARRIVAL_DEADLINE = time(3, 0)

# Defect type -> (day index; -1 = last business date, source, check that must catch it).
DEFECTS = {
    "duplicate_order_id": (1, "orders", "unique_order_id"),
    "orphan_payment": (2, "payments", "payment_order_exists"),
    "null_payment_amount": (3, "payments", "amount_not_null"),
    "renamed_column": (4, "orders", "schema"),
    "late_file": (-1, "orders", "freshness"),
}
RENAMED_COLUMN = ("amount", "order_amount")


@dataclass(frozen=True)
class Options:
    seed: int = DEFAULT_SEED
    reference_date: date = DEFAULT_REFERENCE_DATE
    days: int = DEFAULT_DAYS
    defects: bool = False

    def __post_init__(self) -> None:
        if not 1 <= self.days <= MAX_DAYS:
            raise ValueError(f"days must be between 1 and {MAX_DAYS}")
        if self.defects and self.days < MIN_DAYS_WITH_DEFECTS:
            raise ValueError(f"defects need at least {MIN_DAYS_WITH_DEFECTS} business dates")

    @property
    def business_dates(self) -> list[date]:
        start = self.reference_date - timedelta(days=self.days)
        return [start + timedelta(days=i) for i in range(self.days)]


@dataclass
class SourceFile:
    path: str
    source: str
    fmt: str
    business_date: date
    header: tuple[str, ...]
    rows: list[dict]
    id_field: str
    expected_by: datetime
    arrived_at: datetime


@dataclass
class FixtureSet:
    options: Options
    files: dict[str, bytes] = field(default_factory=dict)
    manifest: dict = field(default_factory=dict)

    def all_files(self) -> dict[str, bytes]:
        return {**self.files, "manifest.json": _json_bytes(self.manifest)}


def _ts(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def _money(cents: int) -> Decimal:
    return Decimal(cents).scaleb(-2)


def _json_bytes(value: object) -> bytes:
    return (json.dumps(value, indent=2, ensure_ascii=False) + "\n").encode("utf-8")


def _arrival(rng: random.Random, business_date: date) -> tuple[datetime, datetime]:
    next_day = business_date + timedelta(days=1)
    expected_by = datetime.combine(next_day, ARRIVAL_DEADLINE, tzinfo=timezone.utc)
    arrived_at = datetime.combine(next_day, time(0, 30), tzinfo=timezone.utc) + timedelta(
        minutes=rng.randint(0, 120), seconds=rng.randint(0, 59)
    )
    return expected_by, arrived_at


def _build_clean(opts: Options) -> list[SourceFile]:
    rng = random.Random(opts.seed)
    dates = opts.business_dates

    customers = []
    for i in range(1, CUSTOMER_COUNT + 1):
        given, family = rng.choice(GIVEN_NAMES), rng.choice(FAMILY_NAMES)
        customers.append(
            {
                "customer_id": f"CUS-{i:04d}",
                "customer_name": f"{given} {family}",
                "email": f"{given}.{family}{i}@{rng.choice(EMAIL_DOMAINS)}".lower(),
                "country": rng.choice(COUNTRIES),
                "segment": rng.choice(SEGMENTS),
                "signup_date": (dates[0] - timedelta(days=rng.randint(30, 720))).isoformat(),
            }
        )

    merchants = [f"{a} {b}" for a, b in rng.sample([(a, b) for a in MERCHANT_WORDS for b in MERCHANT_SUFFIXES], MERCHANT_COUNT)]
    products = [
        (f"{a} {b}", rng.randint(299, 12999))
        for a, b in rng.sample([(a, b) for a in PRODUCT_WORDS for b in PRODUCT_NOUNS], PRODUCT_COUNT)
    ]

    files: list[SourceFile] = []
    for day in dates:
        midnight = datetime.combine(day, time(0), tzinfo=timezone.utc)
        end_of_day = midnight + timedelta(days=1, seconds=-1)
        offsets = sorted(rng.randrange(86400) for _ in range(rng.randint(*ORDERS_PER_DAY)))

        orders, payments = [], []
        for n, offset in enumerate(offsets, start=1):
            ordered_at = midnight + timedelta(seconds=offset)
            product, price_cents = rng.choice(products)
            quantity = rng.randint(1, 4)
            amount = _money(price_cents * quantity)
            status = "cancelled" if rng.random() < CANCEL_RATE else "completed"
            order = {
                "order_id": f"ORD-{day:%Y%m%d}-{n:04d}",
                "customer_id": rng.choice(customers)["customer_id"],
                "merchant": rng.choice(merchants),
                "product": product,
                "quantity": str(quantity),
                "unit_price": str(_money(price_cents)),
                "amount": str(amount),
                "currency": CURRENCY,
                "status": status,
                "ordered_at": _ts(ordered_at),
            }
            orders.append(order)
            if status == "completed":
                paid_at = min(ordered_at + timedelta(seconds=rng.randint(5, 900)), end_of_day)
                payments.append(
                    {
                        "order_id": order["order_id"],
                        "method": rng.choice(PAYMENT_METHODS),
                        "amount": str(amount),
                        "currency": CURRENCY,
                        "status": "captured",
                        "paid_at": paid_at,
                    }
                )

        payments.sort(key=lambda p: (p["paid_at"], p["order_id"]))
        payments = [
            {"payment_id": f"PAY-{day:%Y%m%d}-{n:04d}", **p, "paid_at": _ts(p["paid_at"])}
            for n, p in enumerate(payments, start=1)
        ]
        payments = [{k: p[k] for k in PAYMENT_FIELDS} for p in payments]

        for source, fmt, rows, header, id_field, path in (
            ("orders", "csv", orders, ORDER_COLUMNS, "order_id", f"orders/orders_{day}.csv"),
            ("payments", "jsonl", payments, PAYMENT_FIELDS, "payment_id", f"payments/payments_{day}.jsonl"),
        ):
            expected_by, arrived_at = _arrival(rng, day)
            files.append(SourceFile(path, source, fmt, day, header, rows, id_field, expected_by, arrived_at))

    expected_by, arrived_at = _arrival(rng, dates[-1])
    files.insert(
        0,
        SourceFile(
            CUSTOMERS_PATH, "customers", "csv", dates[-1], CUSTOMER_COLUMNS, customers,
            "customer_id", expected_by, arrived_at,
        ),
    )
    return files


def _apply_defects(opts: Options, files: list[SourceFile]) -> list[dict]:
    rng = random.Random(f"defects:{opts.seed}")
    dates = opts.business_dates
    by_key = {(f.source, f.business_date): f for f in files}
    defects = []

    for kind, (day_index, source, check) in DEFECTS.items():
        target = by_key[(source, dates[day_index])]
        ids = [row[target.id_field] for row in target.rows]
        entry = {"type": kind, "file": target.path, "check": check, "scope": "records"}

        if kind == "duplicate_order_id":
            picked = sorted(rng.sample(range(len(target.rows)), 2))
            for offset, index in enumerate(picked):
                position = index + offset
                target.rows.insert(position + 1, dict(target.rows[position]))
            entry["records"] = [ids[i] for i in picked]
        elif kind == "orphan_payment":
            picked = sorted(rng.sample(range(len(target.rows)), 2))
            for k, index in enumerate(picked):
                target.rows[index]["order_id"] = f"ORD-{dates[day_index]:%Y%m%d}-{9000 + k:04d}"
            entry["records"] = [ids[i] for i in picked]
        elif kind == "null_payment_amount":
            picked = sorted(rng.sample(range(len(target.rows)), 2))
            for index in picked:
                target.rows[index]["amount"] = None
            entry["records"] = [ids[i] for i in picked]
        elif kind == "renamed_column":
            old, new = RENAMED_COLUMN
            target.header = tuple(new if c == old else c for c in target.header)
            entry.update(scope="file", column=old, renamed_to=new, records=ids)
        elif kind == "late_file":
            target.arrived_at = target.expected_by + timedelta(hours=26, minutes=rng.randint(0, 59))
            entry.update(scope="file", records=ids)
        defects.append(entry)
    return defects


def _render(f: SourceFile) -> bytes:
    if f.fmt == "csv":
        buf = io.StringIO(newline="")
        writer = csv.writer(buf, lineterminator="\n")
        writer.writerow(f.header)
        source_columns = ORDER_COLUMNS if f.source == "orders" else CUSTOMER_COLUMNS
        writer.writerows([row[c] for c in source_columns] for row in f.rows)
        return buf.getvalue().encode("utf-8")
    return "".join(
        json.dumps({k: row[k] for k in f.header}, separators=(",", ":"), ensure_ascii=False) + "\n"
        for row in f.rows
    ).encode("utf-8")


def generate(opts: Options | None = None) -> FixtureSet:
    """Build a fixture set in memory."""
    opts = opts or Options()
    files = _build_clean(opts)
    defects = _apply_defects(opts, files) if opts.defects else []

    fx = FixtureSet(opts)
    entries = []
    for f in files:
        data = _render(f)
        fx.files[f.path] = data
        entries.append(
            {
                "path": f.path,
                "source": f.source,
                "format": f.fmt,
                "business_date": f.business_date.isoformat(),
                "record_count": len(f.rows),
                "sha256": hashlib.sha256(data).hexdigest(),
                "expected_by": _ts(f.expected_by),
                "arrived_at": _ts(f.arrived_at),
            }
        )
    dates = opts.business_dates
    fx.manifest = {
        "fixture_version": FIXTURE_VERSION,
        "seed": opts.seed,
        "reference_date": opts.reference_date.isoformat(),
        "business_dates": {"start": dates[0].isoformat(), "end": dates[-1].isoformat()},
        "defects_enabled": opts.defects,
        "files": entries,
        "defects": defects,
    }
    return fx


def write(fx: FixtureSet, out_dir: Path) -> list[Path]:
    """Write a fixture set into an empty (or missing) directory."""
    out_dir = Path(out_dir)
    if out_dir.exists() and any(out_dir.iterdir()):
        raise FileExistsError(f"{out_dir} is not empty")
    written = []
    for rel, data in fx.all_files().items():
        path = out_dir / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        written.append(path)
    return written
