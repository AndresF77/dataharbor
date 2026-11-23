import json
import logging

import pytest

from dataharbor_worker.api_client import ApiError
from dataharbor_worker.ingestion import (
    RAW_DATASETS,
    SourceError,
    close_run,
    entry_for,
    ingest,
    land_dataset,
    load_manifest,
    open_run,
    task_id_for,
    trigger_for,
)

RUN_ID = "scheduled__2026-02-24T03:00:00+00:00"


def tasks(summary) -> dict[str, dict]:
    return {t["task_id"]: t for t in summary.run["tasks"]}


def landed_count(api, dataset_id: str) -> int:
    url = f"/v1/workspaces/{api.workspace_id}/datasets/{dataset_id}/files"
    headers = {"Authorization": f"Bearer {api.tokens['viewer']}"}
    return len(api.get(url, headers=headers).json()["items"])


def test_clean_date_lands_orders_and_payments(worker_client, make_sources):
    sources, options = make_sources()
    business_date = options.business_dates[0]

    summary = ingest(worker_client, sources, business_date, RUN_ID, "scheduled")

    assert summary.status == "succeeded"
    assert summary.run["business_date"] == business_date.isoformat()
    assert summary.run["trigger"] == "scheduled"
    by_task = tasks(summary)
    assert by_task["land_raw_orders"]["status"] == "succeeded"
    assert by_task["land_raw_orders"]["landed_file_id"]
    assert by_task["land_raw_payments"]["status"] == "succeeded"
    # The customers snapshot is only delivered for the last business date.
    assert by_task["land_raw_customers"]["status"] == "skipped"


def test_last_date_lands_customer_snapshot(worker_client, make_sources):
    sources, options = make_sources()

    summary = ingest(worker_client, sources, options.business_dates[-1], RUN_ID)

    assert {t["status"] for t in summary.run["tasks"]} == {"succeeded"}


def test_rerun_reuses_run_and_lands_nothing_new(api, worker_client, make_sources):
    sources, options = make_sources()
    business_date = options.business_dates[0]
    first = ingest(worker_client, sources, business_date, RUN_ID, "scheduled")
    orders_id = tasks(first)["land_raw_orders"]["dataset_id"]

    again = ingest(worker_client, sources, business_date, RUN_ID, "scheduled")

    assert again.run["id"] == first.run["id"]
    assert again.run["tasks"] == first.run["tasks"]
    assert again.run["finished_at"] == first.run["finished_at"]
    assert landed_count(api, orders_id) == 1


def test_retried_task_reports_same_outcome(worker_client, make_sources):
    sources, options = make_sources()
    ctx = open_run(worker_client, RUN_ID, options.business_dates[0], "scheduled")

    first = land_dataset(ctx, sources, "raw_orders")
    retry = land_dataset(ctx, sources, "raw_orders")

    assert retry == first


def test_contract_violation_fails_task_and_run(api, worker_client, make_sources):
    sources, options = make_sources(defects=True)
    renamed_date = options.business_dates[4]  # `amount` renamed in this orders file

    summary = ingest(worker_client, sources, renamed_date, RUN_ID)

    assert summary.status == "failed"
    orders = tasks(summary)["land_raw_orders"]
    assert orders["status"] == "failed"
    assert orders["landed_file_id"] is None
    assert "contract v1" in orders["message"]
    assert "missing_column" in orders["message"]
    assert tasks(summary)["land_raw_payments"]["status"] == "succeeded"
    assert summary.run["error_summary"] == "1 task(s) failed: land_raw_orders"
    assert landed_count(api, orders["dataset_id"]) == 0


@pytest.mark.parametrize(("index", "dataset", "rule"), [
    (1, "raw_orders", "duplicate_key"),
    (3, "raw_payments", "null_value"),
])
def test_other_defects_are_caught_by_contracts(worker_client, make_sources, index, dataset, rule):
    sources, options = make_sources(defects=True)

    summary = ingest(worker_client, sources, options.business_dates[index], RUN_ID)

    task = tasks(summary)[task_id_for(dataset)]
    assert task["status"] == "failed"
    assert rule in task["message"]


def test_changed_file_is_refused_by_checksum(worker_client, make_sources):
    sources, options = make_sources()
    business_date = options.business_dates[0]
    entry = entry_for(load_manifest(sources), "raw_orders", business_date)
    path = sources / entry.path
    path.write_bytes(path.read_bytes() + b"\n")  # any change after delivery

    summary = ingest(worker_client, sources, business_date, RUN_ID)

    orders = tasks(summary)["land_raw_orders"]
    assert orders["status"] == "failed"
    assert "X-Content-SHA256" in orders["message"]


def test_missing_file_fails_task(worker_client, make_sources):
    sources, options = make_sources()
    business_date = options.business_dates[0]
    (sources / entry_for(load_manifest(sources), "raw_payments", business_date).path).unlink()

    summary = ingest(worker_client, sources, business_date, RUN_ID)

    assert tasks(summary)["land_raw_payments"]["status"] == "failed"
    assert "missing" in tasks(summary)["land_raw_payments"]["message"]
    assert summary.status == "failed"


def test_manifest_path_cannot_escape_source_dir(worker_client, make_sources):
    sources, options = make_sources()
    manifest = json.loads((sources / "manifest.json").read_text())
    for entry in manifest["files"]:
        if entry["source"] == "orders":
            entry["path"] = "../outside.csv"
    (sources / "manifest.json").write_text(json.dumps(manifest))

    summary = ingest(worker_client, sources, options.business_dates[0], RUN_ID)

    assert "escapes" in tasks(summary)["land_raw_orders"]["message"]


def test_missing_manifest_is_a_source_error(tmp_path):
    with pytest.raises(SourceError):
        load_manifest(tmp_path)


def test_unreported_task_fails_the_run(worker_client, make_sources):
    sources, options = make_sources()
    ctx = open_run(worker_client, RUN_ID, options.business_dates[0], "scheduled")
    land_dataset(ctx, sources, "raw_orders")

    run = close_run(ctx, [task_id_for(n) for n in RAW_DATASETS])

    assert run["status"] == "failed"
    assert "did not report" in run["error_summary"]
    assert "land_raw_customers" in run["error_summary"]


def test_access_errors_are_raised_for_retry(api, make_sources):
    from dataharbor_worker.api_client import ApiClient
    from tests.conftest import TestClientTransport

    sources, options = make_sources()
    viewer = ApiClient(TestClientTransport(api), api.tokens["viewer"], api.workspace_id)

    with pytest.raises(ApiError) as caught:
        ingest(viewer, sources, options.business_dates[0], RUN_ID)
    assert caught.value.status == 403


@pytest.mark.parametrize(("run_type", "trigger"), [
    ("scheduled", "scheduled"),
    ("manual", "manual"),
    ("backfill", "backfill"),
    ("dataset_triggered", "scheduled"),
    ("something_new", "manual"),
])
def test_trigger_mapping(run_type, trigger):
    assert trigger_for(run_type) == trigger


def test_logs_do_not_contain_file_contents(worker_client, make_sources, caplog):
    sources, options = make_sources()
    entry = entry_for(load_manifest(sources), "raw_orders", options.business_dates[0])
    first_order_line = (sources / entry.path).read_text().splitlines()[1]

    with caplog.at_level(logging.DEBUG):
        ingest(worker_client, sources, options.business_dates[0], RUN_ID)

    assert first_order_line not in caplog.text
