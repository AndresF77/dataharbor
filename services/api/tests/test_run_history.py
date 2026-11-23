import base64

import pytest

from tests.helpers import add_user, auth
from tests.test_pipelines import finish, pipeline_id, pipelines_url, report, runs_url, start


def history(client, demo, role="viewer", **params):
    return client.get(runs_url(client, demo), params=params, headers=auth(demo["tokens"][role]))


def make_run(client, demo, day, *, attempt=1, trigger="scheduled", outcome=None, tasks=()):
    response = start(
        client, demo, external_run_id=f"{trigger}__{day}#{attempt}", business_date=day,
        trigger=trigger,
    )
    assert response.status_code == 201
    run_id = response.json()["id"]
    for task_id, status in tasks:
        assert report(client, demo, run_id, task_id=task_id, status=status).status_code == 201
    if outcome is not None:
        assert finish(client, demo, run_id, outcome).status_code == 200
    return run_id


@pytest.fixture
def runs(client, demo):
    """Five runs over three business dates; 2026-03-02 was retried."""
    return {
        "mar01": make_run(client, demo, "2026-03-01", outcome="succeeded",
                          tasks=[("land_raw_orders", "succeeded"), ("land_raw_customers", "skipped")]),
        "mar02_first": make_run(client, demo, "2026-03-02", outcome="failed",
                                tasks=[("land_raw_orders", "failed"), ("land_raw_payments", "succeeded")]),
        "mar02_retry": make_run(client, demo, "2026-03-02", attempt=2, trigger="manual",
                                outcome="succeeded", tasks=[("land_raw_orders", "succeeded")]),
        "mar03": make_run(client, demo, "2026-03-03"),
        "feb27_backfill": make_run(client, demo, "2026-02-27", trigger="backfill", outcome="succeeded"),
    }


def test_history_is_empty_before_any_run(client, demo):
    response = history(client, demo)

    assert response.status_code == 200
    assert response.json() == {"items": [], "next_cursor": None}


def test_history_lists_newest_business_date_first(client, demo, runs):
    items = history(client, demo).json()["items"]

    assert [i["business_date"] for i in items] == [
        "2026-03-03", "2026-03-02", "2026-03-02", "2026-03-01", "2026-02-27",
    ]
    assert {i["id"] for i in items[1:3]} == {runs["mar02_first"], runs["mar02_retry"]}


def test_history_summarises_each_run(client, demo, runs):
    items = {i["id"]: i for i in history(client, demo).json()["items"]}

    failed = items[runs["mar02_first"]]
    assert failed["status"] == "failed"
    assert failed["task_counts"] == {"succeeded": 1, "failed": 1, "skipped": 0}
    assert failed["duration_seconds"] >= 0
    assert "tasks" not in failed

    assert items[runs["mar01"]]["task_counts"] == {"succeeded": 1, "failed": 0, "skipped": 1}

    running = items[runs["mar03"]]
    assert running["status"] == "running"
    assert running["finished_at"] is None
    assert running["duration_seconds"] is None
    assert running["task_counts"] == {"succeeded": 0, "failed": 0, "skipped": 0}


@pytest.mark.parametrize(
    ("params", "expected"),
    [
        ({"status": "failed"}, ["mar02_first"]),
        ({"status": "running"}, ["mar03"]),
        ({"trigger": "backfill"}, ["feb27_backfill"]),
        ({"trigger": "manual", "status": "succeeded"}, ["mar02_retry"]),
        ({"business_date_from": "2026-03-01", "business_date_to": "2026-03-01"}, ["mar01"]),
        ({"business_date_to": "2026-02-28"}, ["feb27_backfill"]),
        ({"business_date_from": "2026-03-02", "status": "succeeded"}, ["mar02_retry"]),
        ({"business_date_from": "2026-04-01"}, []),
    ],
)
def test_history_filters(client, demo, runs, params, expected):
    items = history(client, demo, **params).json()["items"]

    assert sorted(i["id"] for i in items) == sorted(runs[name] for name in expected)


def test_history_pages_never_skip_or_repeat(client, demo, runs):
    full = [i["id"] for i in history(client, demo).json()["items"]]

    seen, cursor = [], None
    while True:
        params = {"limit": 2} | ({"cursor": cursor} if cursor else {})
        page = history(client, demo, **params).json()
        assert len(page["items"]) <= 2
        seen += [i["id"] for i in page["items"]]
        cursor = page["next_cursor"]
        if cursor is None:
            break

    assert seen == full
    assert len(set(seen)) == len(runs)


def test_history_pages_respect_filters(client, demo, runs):
    first = history(client, demo, status="succeeded", limit=2).json()
    second = history(client, demo, status="succeeded", limit=2, cursor=first["next_cursor"]).json()

    ids = [i["id"] for i in first["items"] + second["items"]]
    assert sorted(ids) == sorted([runs["mar01"], runs["mar02_retry"], runs["feb27_backfill"]])
    assert second["next_cursor"] is None


def encode(raw: str) -> str:
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


@pytest.mark.parametrize(
    "params",
    [
        {"cursor": "not base64!"},
        {"cursor": encode("2026-03-01")},
        {"cursor": encode("2026-13-01|00000000-0000-0000-0000-000000000000")},
        {"cursor": encode("2026-03-01|not-a-uuid")},
        {"cursor": "x" * 129},
        {"limit": 0},
        {"limit": 201},
        {"status": "paused"},
        {"trigger": "cron"},
        {"business_date_from": "2026-03-02", "business_date_to": "2026-03-01"},
    ],
)
def test_history_validation(client, demo, params):
    assert history(client, demo, **params).status_code == 422


def test_history_access(client, demo, runs):
    _, outsider = add_user(client, "olga.outsider@example.test")
    url = runs_url(client, demo)

    assert client.get(url).status_code == 401
    assert client.get(url, headers=auth(outsider)).status_code == 404
    for role in ("viewer", "engineer", "admin"):
        assert history(client, demo, role=role).status_code == 200


def test_history_of_unknown_or_foreign_pipeline_is_not_found(client, demo, runs):
    _, token = add_user(client, "wanda.other@example.test")
    other = client.post("/v1/workspaces", json={"slug": "other-co", "name": "Other"},
                        headers=auth(token)).json()
    foreign = f"/v1/workspaces/{other['id']}/pipelines/{pipeline_id(client, demo)}/runs"
    unknown = f"{pipelines_url(demo)}/00000000-0000-0000-0000-000000000000/runs"

    assert client.get(foreign, headers=auth(token)).status_code == 404
    assert client.get(unknown, headers=auth(demo["tokens"]["viewer"])).status_code == 404
