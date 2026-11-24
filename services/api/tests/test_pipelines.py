import logging

import pytest

from tests.helpers import add_user, auth
from tests.test_landing import dataset_id, land

DATE = "2026-03-01"
EXTERNAL_ID = "scheduled__2026-03-01T03:00:00+00:00"


def pipelines_url(demo) -> str:
    return f"/v1/workspaces/{demo['workspace_id']}/pipelines"


def pipeline_id(client, demo, name: str = "ingest_raw_sources") -> str:
    items = client.get(pipelines_url(demo), headers=auth(demo["tokens"]["viewer"])).json()["items"]
    return next(p["id"] for p in items if p["name"] == name)


def runs_url(client, demo, suffix: str = "") -> str:
    return f"{pipelines_url(demo)}/{pipeline_id(client, demo)}/runs{suffix}"


def start(client, demo, role="engineer", **overrides):
    body = {"external_run_id": EXTERNAL_ID, "business_date": DATE, "trigger": "scheduled"}
    body.update(overrides)
    return client.post(runs_url(client, demo), json=body, headers=auth(demo["tokens"][role]))


def report(client, demo, run_id, role="engineer", **body):
    return client.post(
        runs_url(client, demo, f"/{run_id}/tasks"), json=body, headers=auth(demo["tokens"][role])
    )


def finish(client, demo, run_id, status, role="engineer", **extra):
    return client.post(
        runs_url(client, demo, f"/{run_id}/finish"),
        json={"status": status, **extra},
        headers=auth(demo["tokens"][role]),
    )


def test_seeded_pipeline_is_listed(client, demo):
    response = client.get(pipelines_url(demo), headers=auth(demo["tokens"]["viewer"]))

    assert response.status_code == 200
    [pipeline] = response.json()["items"]
    assert pipeline["name"] == "ingest_raw_sources"
    assert pipeline["kind"] == "ingestion"
    assert pipeline["dag_id"] == "dataharbor_ingest_raw_sources"
    assert pipeline["schedule"] == "0 3 * * *"


def test_pipelines_need_membership(client, demo):
    _, outsider = add_user(client, "olga.outsider@example.test")

    assert client.get(pipelines_url(demo)).status_code == 401
    assert client.get(pipelines_url(demo), headers=auth(outsider)).status_code == 404


def test_start_run_returns_stable_id(client, demo):
    response = start(client, demo)

    assert response.status_code == 201
    run = response.json()
    assert run["status"] == "running"
    assert run["business_date"] == DATE
    assert run["trigger"] == "scheduled"
    assert run["finished_at"] is None
    assert run["tasks"] == []

    again = start(client, demo)
    assert again.status_code == 200
    assert again.json()["id"] == run["id"]


def test_restart_with_different_date_conflicts(client, demo):
    start(client, demo)

    response = start(client, demo, business_date="2026-03-02")

    assert response.status_code == 409


def test_viewer_cannot_report_runs(client, demo):
    assert start(client, demo, role="viewer").status_code == 403


@pytest.mark.parametrize(
    "overrides",
    [
        {"external_run_id": ""},
        {"external_run_id": "run\nid"},
        {"trigger": "cron"},
        {"business_date": "yesterday"},
        {"unexpected": 1},
    ],
)
def test_start_run_validation(client, demo, overrides):
    assert start(client, demo, **overrides).status_code == 422


def test_full_run_with_landed_file(client, demo):
    landed = land(client, demo).json()
    orders = dataset_id(client, demo, "raw_orders")
    run_id = start(client, demo).json()["id"]

    recorded = report(
        client, demo, run_id, task_id="land_raw_orders", status="succeeded",
        dataset_id=orders, landed_file_id=landed["id"], message="2 records",
    )
    assert recorded.status_code == 201
    [task] = recorded.json()["tasks"]
    assert task["landed_file_id"] == landed["id"]

    done = finish(client, demo, run_id, "succeeded")
    assert done.status_code == 200
    assert done.json()["status"] == "succeeded"
    assert done.json()["finished_at"] is not None

    viewed = client.get(runs_url(client, demo, f"/{run_id}"), headers=auth(demo["tokens"]["viewer"]))
    assert viewed.status_code == 200
    assert viewed.json()["tasks"][0]["task_id"] == "land_raw_orders"


def test_task_outcome_is_written_once(client, demo):
    run_id = start(client, demo).json()["id"]
    body = {"task_id": "land_raw_orders", "status": "failed", "message": "3 violations"}

    assert report(client, demo, run_id, **body).status_code == 201
    assert report(client, demo, run_id, **body).status_code == 200
    changed = report(client, demo, run_id, **{**body, "status": "succeeded"})
    assert changed.status_code == 409

    run = client.get(runs_url(client, demo, f"/{run_id}"), headers=auth(demo["tokens"]["viewer"]))
    assert [t["status"] for t in run.json()["tasks"]] == ["failed"]


def test_run_with_failed_task_cannot_succeed(client, demo):
    run_id = start(client, demo).json()["id"]
    report(client, demo, run_id, task_id="land_raw_payments", status="failed")

    assert finish(client, demo, run_id, "succeeded").status_code == 409
    failed = finish(client, demo, run_id, "failed", error_summary="1 of 1 task failed")
    assert failed.status_code == 200
    assert failed.json()["error_summary"] == "1 of 1 task failed"


def test_finished_run_is_immutable(client, demo):
    run_id = start(client, demo).json()["id"]
    finish(client, demo, run_id, "failed", error_summary="source missing")

    assert finish(client, demo, run_id, "failed", error_summary="source missing").status_code == 200
    assert finish(client, demo, run_id, "succeeded").status_code == 409
    late = report(client, demo, run_id, task_id="land_raw_orders", status="succeeded")
    assert late.status_code == 409


@pytest.mark.parametrize(
    ("status", "extra"),
    [("running", {}), ("succeeded", {"error_summary": "nope"}), ("failed", {"error_summary": ""})],
)
def test_finish_validation(client, demo, status, extra):
    run_id = start(client, demo).json()["id"]

    assert finish(client, demo, run_id, status, **extra).status_code == 422


def test_task_references_are_checked(client, demo):
    landed = land(client, demo).json()
    payments = dataset_id(client, demo, "raw_payments")
    run_id = start(client, demo).json()["id"]

    no_dataset = report(client, demo, run_id, task_id="t1", status="succeeded",
                        landed_file_id=landed["id"])
    wrong_dataset = report(client, demo, run_id, task_id="t2", status="succeeded",
                           dataset_id=payments, landed_file_id=landed["id"])
    unknown_dataset = report(client, demo, run_id, task_id="t3", status="succeeded",
                             dataset_id="00000000-0000-0000-0000-000000000000")

    assert no_dataset.status_code == 422
    assert wrong_dataset.status_code == 422
    assert unknown_dataset.status_code == 422


def test_task_message_is_bounded(client, demo):
    run_id = start(client, demo).json()["id"]

    response = report(client, demo, run_id, task_id="t1", status="failed", message="x" * 501)

    assert response.status_code == 422


def test_run_from_other_workspace_is_not_found(client, demo):
    run_id = start(client, demo).json()["id"]
    _, token = add_user(client, "wanda.other@example.test")
    other = client.post("/v1/workspaces", json={"slug": "other-co", "name": "Other"},
                        headers=auth(token)).json()

    response = client.get(
        f"/v1/workspaces/{other['id']}/pipelines/{pipeline_id(client, demo)}/runs/{run_id}",
        headers=auth(token),
    )

    assert response.status_code == 404


def test_run_logs_ids_not_messages(client, demo, caplog):
    run_id = start(client, demo).json()["id"]
    with caplog.at_level(logging.INFO, logger="dataharbor_api.pipelines"):
        report(client, demo, run_id, task_id="t1", status="failed", message="secret=hunter2")

    assert run_id in caplog.text
    assert "hunter2" not in caplog.text

