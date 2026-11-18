# dataharbor-fixtures

Deterministic synthetic source data (orders, payments, customers) for development, tests
and the demo, following [ADR 0001](../../docs/adr/0001-local-data-platform-and-fixtures.md).
Python 3.11+, standard library only.

```bash
cd packages/fixtures
python3 -m dataharbor_fixtures generate --out out/clean               # seed 1042, reference date 2026-03-02, 7 business dates
python3 -m dataharbor_fixtures generate --out out/defects --defects   # same data plus the quality-check defects
python3 -m dataharbor_fixtures generate --out /tmp/fx --seed 7 --days 14 --reference-date 2026-04-01
python3 -m dataharbor_fixtures check out/defects                      # lists findings; exit code 1 if any
python3 -m unittest discover -s tests -t .                            # acceptance tests AC-01.6 to AC-01.13
```

The same options always produce byte-identical files. `generate` refuses a non-empty
output directory, so stale files never mix into a set. `out/` is ignored by git.

## Output

Business dates are the `--days` UTC dates before the reference date.

| Path | Format | Contents |
|---|---|---|
| `customers/customers_snapshot.csv` | CSV | 40 customers as of the last business date |
| `orders/orders_<date>.csv` | CSV | 18–30 orders placed on that date |
| `payments/payments_<date>.jsonl` | JSON lines | one captured payment per completed order of that date |
| `manifest.json` | JSON | `fixture_version`, seed, reference date, business date range, every file with its record count, SHA-256, `expected_by` and `arrived_at`, and the injected defects |

Column order is fixed:

- **customers:** `customer_id, customer_name, email, country, segment, signup_date`
- **orders:** `order_id, customer_id, merchant, product, quantity, unit_price, amount, currency, status, ordered_at`
- **payments:** `payment_id, order_id, method, amount, currency, status, paid_at`

Amounts are decimal strings with two places (also in JSON, to avoid floats). Timestamps are
UTC with an explicit `+00:00` offset. Cancelled orders (about 8 %) have no payment. Each
file is expected by 03:00 UTC the day after its business date; `arrived_at` simulates when
it landed.

## Defects

`--defects` applies these on top of the clean set, each to a different file. Files they do
not touch are byte-identical to the clean set. Each one is listed in `manifest.json` with
its file, the affected record IDs and the check that must catch it.

| Type | What is injected | Caught by |
|---|---|---|
| `duplicate_order_id` | two orders delivered twice (2nd business date) | `unique_order_id` |
| `orphan_payment` | two payments pointing at order IDs that do not exist (3rd) | `payment_order_exists` |
| `null_payment_amount` | two payments with a `null` amount (4th) | `amount_not_null` |
| `renamed_column` | `amount` renamed to `order_amount` in one orders file (5th) | `schema` |
| `late_file` | the last orders file arrives more than 24 h after `expected_by` | `freshness` |

`check` runs these checks plus manifest integrity (`checksum`, `record_count`,
`missing_file`, `unlisted_file`), customer references, amount format and email domains.
A clean set has no findings; a defect set has exactly the ones listed in its manifest.

## Fictional data

Customer, merchant and product names are built only from the word lists in
`generator.py`. Emails use `example.com` or `example.org`. Payments carry only a method
(`card`, `bank_transfer`, `wallet`), never card numbers or account identifiers.

## Versioning

Change `FIXTURE_VERSION` whenever the output for the same options changes (new columns,
new defects, different distributions), so runs and test snapshots can record which
version they used.
