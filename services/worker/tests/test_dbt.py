"""dbt models and their tests, run as part of the worker test suite.

`dbt parse` runs whenever a `dbt` executable is available (on PATH or
`DATAHARBOR_TEST_DBT`); it needs no database. The `dbt build` tests also need
a scratch PostgreSQL database (`DATAHARBOR_TEST_WAREHOUSE_*`): they land the
synthetic fixtures through the in-process API, load them with `load-raw` and
build every model and test.
"""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from dataharbor_worker.ingestion import ingest
from dataharbor_worker.warehouse import load_business_date

DBT_DIR = Path(__file__).resolve().parents[1] / "dbt"
MODELS = ("stg_orders", "stg_payments", "daily_revenue")
SINGULAR_TESTS = (
    "assert_order_amount_is_quantity_times_price",
    "assert_orphan_flag_matches_orders",
    "assert_daily_revenue_reconciles_with_staging",
    "assert_paid_orders_within_completed",
)


@pytest.fixture(scope="session")
def dbt_executable() -> str:
    executable = os.environ.get("DATAHARBOR_TEST_DBT") or shutil.which("dbt")
    if not executable:
        pytest.skip("dbt not installed (pip install -e '.[dbt]' or set DATAHARBOR_TEST_DBT)")
    return executable


@pytest.fixture
def run_dbt(dbt_executable, tmp_path):
    """Run a dbt command against the project; returns (exit code, results by node name)."""

    def run(*args: str, env: dict[str, str]) -> tuple[int, dict[str, dict]]:
        target = tmp_path / "dbt-target"
        completed = subprocess.run(
            [dbt_executable, *args, "--profiles-dir", str(DBT_DIR),
             "--target-path", str(target), "--log-path", str(tmp_path / "dbt-logs")],
            cwd=DBT_DIR,
            env={**os.environ, "DBT_SEND_ANONYMOUS_USAGE_STATS": "false", **env},
            capture_output=True,
            text=True,
            timeout=600,
        )
        results_file = target / "run_results.json"
        results = {}
        if results_file.exists():
            for result in json.loads(results_file.read_text())["results"]:
                results[result["unique_id"]] = result
        run.output = completed.stdout + completed.stderr
        run.target = target
        return completed.returncode, results

    return run


def _statuses(results: dict[str, dict], resource: str) -> dict[str, str]:
    prefix = f"{resource}.dataharbor."
    return {uid.removeprefix(prefix): r["status"] for uid, r in results.items()
            if uid.startswith(prefix)}


def test_project_parses_with_tests_on_every_model(run_dbt):
    code, _ = run_dbt("parse", env={"DATAHARBOR_WAREHOUSE_USER": "parse-only",
                                    "DATAHARBOR_WAREHOUSE_PASSWORD": "parse-only"})
    assert code == 0, run_dbt.output

    manifest = json.loads((run_dbt.target / "manifest.json").read_text())
    tests = [n for n in manifest["nodes"].values() if n["resource_type"] == "test"]
    tested_models = {dep.split(".")[-1] for t in tests for dep in t["depends_on"]["nodes"]}
    assert set(MODELS) <= tested_models
    assert set(SINGULAR_TESTS) <= {t["name"] for t in tests}
    assert any(t["name"].startswith("unique_stg_orders_order_id") for t in tests)
    assert any(t["name"].startswith("unique_combination_daily_revenue") for t in tests)


def _land_and_load(client, conn, sources, dates) -> None:
    for day in dates:
        ingest(client, sources, day, f"scheduled__{day}T03:00:00+00:00")
        load_business_date(client, conn, sources, day)


def test_build_passes_on_clean_fixtures(worker_client, make_sources, warehouse,
                                        warehouse_env, run_dbt):
    sources, options = make_sources()
    _land_and_load(worker_client, warehouse, sources, options.business_dates)

    code, results = run_dbt("build", env=warehouse_env)

    assert code == 0, run_dbt.output
    assert _statuses(results, "model") == {m: "success" for m in MODELS}
    tests = _statuses(results, "test")
    assert len(tests) >= 40
    assert set(tests.values()) == {"pass"}
    totals = warehouse.execute(
        "SELECT sum(completed_orders), sum(paid_orders), sum(outstanding_amount),"
        " sum(orphan_payments) FROM mart.daily_revenue").fetchone()
    assert totals[0] > 0 and totals[1] == totals[0]
    assert totals[2] == 0 and totals[3] == 0


def test_build_passes_on_defect_fixtures_with_orphans_counted(
        worker_client, make_sources, warehouse, warehouse_env, run_dbt):
    sources, options = make_sources(defects=True)
    _land_and_load(worker_client, warehouse, sources, options.business_dates)

    code, results = run_dbt("build", env=warehouse_env)

    assert code == 0, run_dbt.output
    assert set(_statuses(results, "test").values()) == {"pass"}
    orphan_day = options.business_dates[2]  # two payments point at unknown orders
    row = warehouse.execute(
        "SELECT orphan_payments, outstanding_amount > 0 FROM mart.daily_revenue"
        " WHERE revenue_date = %s", (orphan_day,)).fetchone()
    assert row == (2, True)
    flagged = warehouse.execute(
        "SELECT count(*) FROM staging.stg_payments WHERE NOT has_known_order").fetchone()[0]
    mart = warehouse.execute("SELECT sum(orphan_payments) FROM mart.daily_revenue").fetchone()[0]
    assert flagged == mart > 2  # plus the payments of days whose orders file was refused


def test_build_fails_when_staged_data_breaks_a_test(worker_client, make_sources, warehouse,
                                                    warehouse_env, run_dbt):
    sources, options = make_sources()
    _land_and_load(worker_client, warehouse, sources, options.business_dates[:1])
    warehouse.execute(
        "INSERT INTO raw.orders (order_id, customer_id, merchant, product, quantity,"
        " unit_price, amount, currency, status, ordered_at, _business_date, _file_sha256,"
        " _record_number) VALUES ('ORD-BAD-1', 'CUS-1', 'Shop', 'Tea', '2', '1.50', '9.99',"
        " 'usd1', 'refunded', '2026-03-01T10:00:00+00:00', '2026-03-01', %s, 1)",
        ("0" * 64,))
    warehouse.commit()

    code, results = run_dbt("build", env=warehouse_env)

    assert code == 1
    failed = {uid.split(".")[2] for uid, r in results.items()
              if r["status"] in ("fail", "error")}
    assert "assert_order_amount_is_quantity_times_price" in failed
    assert any(name.startswith("accepted_values_stg_orders_status") for name in failed)
    assert any(name.startswith("currency_code_stg_orders_currency") for name in failed)
    # Models downstream of a failing test are skipped, not built on bad data.
    assert results["model.dataharbor.daily_revenue"]["status"] == "skipped"
