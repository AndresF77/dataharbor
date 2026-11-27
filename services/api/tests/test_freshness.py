import logging
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from dataharbor_api.freshness import FreshnessStatus, Thresholds, classify, utc_now
from dataharbor_api.models import Pipeline
from tests.helpers import add_user, auth
from tests.test_landing import ORDERS_CSV, base, dataset_id, land
from tests.test_pipelines import finish, pipeline_id, start

T0 = datetime(2026, 3, 2, 3, 0, tzinfo=timezone.utc)


def rule_url(client, demo, name: str, suffix: str = "freshness-rule") -> str:
    return f"{base(demo)}/{dataset_id(client, demo, name)}/{suffix}"


def put_rule(client, demo, name="stg_orders", role="engineer", **body):
    return client.put(rule_url(client, demo, name), json=body, headers=auth(demo["tokens"][role]))


def freshness(client, demo, name, role="viewer"):
    return client.get(rule_url(client, demo, name, "freshness"), headers=auth(demo["tokens"][role]))


def overview(client, demo, role="viewer", **params):
    return client.get(
        f"/v1/workspaces/{demo['workspace_id']}/freshness",
        params=params,
        headers=auth(demo["tokens"][role]),
    )


@pytest.fixture
def clock(client):
    """Pin the evaluation clock: `clock.set(datetime)`."""

    class Clock:
        now = T0

        def set(self, value: datetime) -> None:
            self.now = value

    pinned = Clock()
    client.app.dependency_overrides[utc_now] = lambda: pinned.now
    yield pinned
    client.app.dependency_overrides.pop(utc_now, None)


def parse(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


# --- classify: the pure rule ---------------------------------------------------------


@pytest.mark.parametrize(
    ("age", "expected"),
    [
        (timedelta(0), FreshnessStatus.FRESH),
        (timedelta(minutes=60), FreshnessStatus.FRESH),  # exactly warn: not yet
        (timedelta(minutes=60, seconds=1), FreshnessStatus.WARNING),
        (timedelta(minutes=120), FreshnessStatus.WARNING),  # exactly error: not yet
        (timedelta(minutes=120, seconds=1), FreshnessStatus.STALE),
        (timedelta(days=400), FreshnessStatus.STALE),
    ],
)
def test_classify_thresholds_are_exclusive(age, expected):
    status, seconds = classify(Thresholds(60, 120), T0 - age, T0)

    assert status is expected
    assert seconds == int(age.total_seconds())


def test_classify_without_warn_goes_straight_to_stale():
    rule = Thresholds(None, 30)

    assert classify(rule, T0 - timedelta(minutes=29), T0)[0] is FreshnessStatus.FRESH
    assert classify(rule, T0 - timedelta(minutes=31), T0)[0] is FreshnessStatus.STALE


def test_classify_never_updated_is_unknown_and_future_updates_are_age_zero():
    assert classify(Thresholds(1, 2), None, T0) == (FreshnessStatus.UNKNOWN, None)
    assert classify(Thresholds(1, 2), T0 + timedelta(hours=1), T0) == (FreshnessStatus.FRESH, 0)


def test_classify_treats_naive_timestamps_as_utc():
    naive = (T0 - timedelta(minutes=90)).replace(tzinfo=None)

    assert classify(Thresholds(60, 120), naive, T0) == (FreshnessStatus.WARNING, 5400)


# --- rule CRUD -----------------------------------------------------------------------


def test_seed_adds_landed_file_rules_to_raw_datasets(client, demo):
    response = client.get(
        rule_url(client, demo, "raw_orders"), headers=auth(demo["tokens"]["viewer"])
    )

    assert response.status_code == 200
    rule = response.json()
    assert rule["signal"] == "landed_file"
    assert (rule["warn_after_minutes"], rule["error_after_minutes"]) == (1500, 1560)
    assert rule["pipeline_id"] is None
    assert rule["version"] == 1
    no_rule = client.get(
        rule_url(client, demo, "stg_orders"), headers=auth(demo["tokens"]["viewer"])
    )
    assert no_rule.status_code == 404


def test_create_pipeline_rule_defaults_error_to_dataset_sla(client, demo):
    pipeline = pipeline_id(client, demo)

    response = put_rule(client, demo, signal="pipeline_run", pipeline_id=pipeline,
                        warn_after_minutes=1440)

    assert response.status_code == 201
    rule = response.json()
    assert rule["error_after_minutes"] == 1500  # stg_orders SLA from the seed
    assert rule["warn_after_minutes"] == 1440
    assert rule["pipeline_id"] == pipeline
    assert rule["version"] == 1


def test_replace_needs_the_current_version(client, demo):
    pipeline = pipeline_id(client, demo)
    assert put_rule(client, demo, signal="pipeline_run", pipeline_id=pipeline).status_code == 201

    again = put_rule(client, demo, signal="pipeline_run", pipeline_id=pipeline)
    stale = put_rule(client, demo, version=7, signal="pipeline_run", pipeline_id=pipeline)
    replaced = put_rule(client, demo, version=1, signal="pipeline_run", pipeline_id=pipeline,
                        error_after_minutes=90, warn_after_minutes=30)
    lost_update = put_rule(client, demo, version=1, signal="pipeline_run", pipeline_id=pipeline)

    assert again.status_code == 409
    assert "version 1" in again.json()["error"]["message"]
    assert stale.status_code == 409
    assert replaced.status_code == 200
    assert replaced.json()["version"] == 2
    assert (replaced.json()["warn_after_minutes"], replaced.json()["error_after_minutes"]) == (30, 90)
    assert lost_update.status_code == 409
    assert "current version 2" in lost_update.json()["error"]["message"]


def test_versioned_put_without_a_rule_is_a_conflict(client, demo):
    response = put_rule(client, demo, version=1, signal="pipeline_run",
                        pipeline_id=pipeline_id(client, demo))

    assert response.status_code == 409


@pytest.mark.parametrize(
    ("name", "body", "fragment"),
    [
        ("stg_orders", {"signal": "pipeline_run"}, "pipeline_id is required"),
        ("raw_orders", {"signal": "landed_file", "pipeline_id": str(uuid.uuid4())}, "only allowed"),
        ("raw_orders", {"signal": "landed_file", "error_after_minutes": 0}, None),
        ("raw_orders", {"signal": "landed_file", "error_after_minutes": 525601}, None),
        ("raw_orders", {"signal": "landed_file", "warn_after_minutes": 0}, None),
        ("raw_orders", {"signal": "landed_file", "unexpected": 1}, None),
        ("raw_orders", {"signal": "hourly"}, None),
    ],
)
def test_invalid_rule_bodies_are_rejected(client, demo, name, body, fragment):
    response = put_rule(client, demo, name=name, **body)

    assert response.status_code == 422
    if fragment:
        assert fragment in str(response.json()["error"]["details"])


def test_warn_must_be_below_error(client, demo):
    pipeline = pipeline_id(client, demo)

    explicit = put_rule(client, demo, signal="pipeline_run", pipeline_id=pipeline,
                        warn_after_minutes=60, error_after_minutes=60)
    from_sla = put_rule(client, demo, signal="pipeline_run", pipeline_id=pipeline,
                        warn_after_minutes=1500)  # stg_orders SLA is 1500

    assert explicit.status_code == 422
    assert from_sla.status_code == 422
    assert "below" in from_sla.json()["error"]["message"]


def test_error_threshold_is_required_without_an_sla(client, demo):
    dataset = rule_url(client, demo, "stg_orders", "")[:-1]
    current = client.get(dataset, headers=auth(demo["tokens"]["viewer"])).json()
    cleared = client.patch(dataset, json={"version": current["version"],
                                          "freshness_sla_minutes": None},
                           headers=auth(demo["tokens"]["engineer"]))
    assert cleared.status_code == 200

    response = put_rule(client, demo, signal="pipeline_run", pipeline_id=pipeline_id(client, demo))

    assert response.status_code == 422
    assert "freshness SLA" in response.json()["error"]["message"]


def test_landed_file_signal_is_only_for_raw_datasets(client, demo):
    response = put_rule(client, demo, name="daily_revenue", signal="landed_file",
                        error_after_minutes=60)

    assert response.status_code == 422
    assert "raw" in response.json()["error"]["message"]


def test_pipeline_must_belong_to_the_workspace(client, demo):
    other_ws = client.post("/v1/workspaces", json={"slug": "other-team", "name": "Other"},
                           headers=auth(demo["tokens"]["engineer"]))
    assert other_ws.status_code == 201
    with client.app.state.database.new_session() as session:
        foreign = Pipeline(workspace_id=uuid.UUID(other_ws.json()["id"]), name="elsewhere",
                           kind="transformation", dag_id="elsewhere", description="")
        session.add(foreign)
        session.commit()
        foreign_id = str(foreign.id)

    for candidate in (foreign_id, str(uuid.uuid4())):
        response = put_rule(client, demo, signal="pipeline_run", pipeline_id=candidate)
        assert response.status_code == 422
        assert "this workspace" in response.json()["error"]["message"]


def test_roles_and_membership(client, demo):
    _, outsider = add_user(client, "olga.outsider@example.test")
    url = rule_url(client, demo, "raw_orders")
    body = {"version": 1, "signal": "landed_file", "error_after_minutes": 60}

    assert client.get(url).status_code == 401
    assert client.get(url, headers=auth(outsider)).status_code == 404
    assert client.put(url, json=body, headers=auth(outsider)).status_code == 404
    assert client.put(url, json=body, headers=auth(demo["tokens"]["viewer"])).status_code == 403
    assert client.delete(url, headers=auth(demo["tokens"]["viewer"])).status_code == 403
    assert freshness(client, demo, "raw_orders").status_code == 200
    assert overview(client, demo).status_code == 200
    outside = client.get(f"/v1/workspaces/{demo['workspace_id']}/freshness", headers=auth(outsider))
    assert outside.status_code == 404


def test_unknown_dataset_is_404(client, demo):
    url = f"{base(demo)}/{uuid.uuid4()}/freshness-rule"

    assert client.get(url, headers=auth(demo["tokens"]["viewer"])).status_code == 404
    response = client.put(url, json={"signal": "landed_file", "error_after_minutes": 5},
                          headers=auth(demo["tokens"]["engineer"]))
    assert response.status_code == 404


def test_delete_rule(client, demo):
    url = rule_url(client, demo, "raw_orders")

    assert client.delete(url, headers=auth(demo["tokens"]["engineer"])).status_code == 204
    assert client.get(url, headers=auth(demo["tokens"]["viewer"])).status_code == 404
    assert client.delete(url, headers=auth(demo["tokens"]["engineer"])).status_code == 404
    assert freshness(client, demo, "raw_orders").status_code == 404
    names = [i["dataset_name"] for i in overview(client, demo).json()["items"]]
    assert "raw_orders" not in names


# --- evaluation ----------------------------------------------------------------------


def test_raw_dataset_follows_its_newest_landed_file(client, demo, clock):
    assert freshness(client, demo, "raw_orders").json()["status"] == "unknown"

    landed = land(client, demo)
    assert landed.status_code == 201
    landed_at = parse(landed.json()["landed_at"])

    expectations = [
        (timedelta(minutes=10), "fresh"),
        (timedelta(minutes=1500, seconds=1), "warning"),
        (timedelta(minutes=1560, seconds=1), "stale"),
    ]
    for age, expected in expectations:
        clock.set(landed_at + age)
        result = freshness(client, demo, "raw_orders").json()
        assert result["status"] == expected
        assert result["age_seconds"] == int(age.total_seconds())
        assert result["last_business_date"] == "2026-03-01"
        assert parse(result["last_updated_at"]) == landed_at
        assert parse(result["evaluated_at"]) == clock.now


def test_a_newer_landing_makes_the_dataset_fresh_again(client, demo, clock):
    first = parse(land(client, demo).json()["landed_at"])
    clock.set(first + timedelta(days=3))
    assert freshness(client, demo, "raw_orders").json()["status"] == "stale"

    second = land(client, demo, data=ORDERS_CSV + b"o-3,c-3,Harbor Goods,Cedar Mug,1,4.50,4.50,"
                  b"USD,paid,2026-03-02T09:00:00Z\n", business_date="2026-03-02",
                  filename="orders_2026-03-02.csv")
    assert second.status_code == 201
    clock.set(parse(second.json()["landed_at"]) + timedelta(minutes=5))

    result = freshness(client, demo, "raw_orders").json()
    assert result["status"] == "fresh"
    assert result["last_business_date"] == "2026-03-02"


def test_pipeline_rule_counts_only_successful_runs(client, demo, clock):
    pipeline = pipeline_id(client, demo)
    assert put_rule(client, demo, signal="pipeline_run", pipeline_id=pipeline,
                    warn_after_minutes=60, error_after_minutes=120).status_code == 201

    failed = start(client, demo, external_run_id="run-a", business_date="2026-03-01").json()
    assert finish(client, demo, failed["id"], "failed", error_summary="boom").status_code == 200
    running = start(client, demo, external_run_id="run-b", business_date="2026-03-03").json()
    assert running["status"] == "running"
    assert freshness(client, demo, "stg_orders").json()["status"] == "unknown"

    ok = start(client, demo, external_run_id="run-c", business_date="2026-03-02").json()
    done = finish(client, demo, ok["id"], "succeeded").json()
    clock.set(parse(done["finished_at"]) + timedelta(minutes=61))

    result = freshness(client, demo, "stg_orders").json()
    assert result["status"] == "warning"
    assert result["last_business_date"] == "2026-03-02"
    assert result["pipeline_id"] == pipeline


def test_workspace_overview_orders_worst_first_and_filters(client, demo, clock):
    pipeline = pipeline_id(client, demo)
    assert put_rule(client, demo, name="stg_orders", signal="pipeline_run",
                    pipeline_id=pipeline, error_after_minutes=30).status_code == 201
    run = start(client, demo, external_run_id="run-1").json()
    finished_at = parse(finish(client, demo, run["id"], "succeeded").json()["finished_at"])
    landed_at = parse(land(client, demo).json()["landed_at"])
    clock.set(max(finished_at, landed_at) + timedelta(minutes=45))

    body = overview(client, demo).json()

    statuses = [(i["dataset_name"], i["status"]) for i in body["items"]]
    assert statuses == [
        ("stg_orders", "stale"),
        ("raw_customers", "unknown"),
        ("raw_payments", "unknown"),
        ("raw_orders", "fresh"),
    ]
    assert body["counts"] == {"stale": 1, "unknown": 2, "warning": 0, "fresh": 1}
    assert parse(body["evaluated_at"]) == clock.now

    filtered = overview(client, demo, status=["stale", "fresh"]).json()
    assert [i["dataset_name"] for i in filtered["items"]] == ["stg_orders", "raw_orders"]
    assert filtered["counts"] == body["counts"]
    assert overview(client, demo, status="rotten").status_code == 422


def test_overview_is_scoped_to_the_workspace(client, demo):
    other = client.post("/v1/workspaces", json={"slug": "other-team", "name": "Other"},
                        headers=auth(demo["tokens"]["engineer"]))
    other_id = other.json()["id"]
    created = client.post(f"/v1/workspaces/{other_id}/datasets",
                          json={"name": "raw_secret", "layer": "raw",
                                "freshness_sla_minutes": 60},
                          headers=auth(demo["tokens"]["engineer"]))
    assert created.status_code == 201
    url = f"/v1/workspaces/{other_id}/datasets/{created.json()['id']}/freshness-rule"
    assert client.put(url, json={"signal": "landed_file"},
                      headers=auth(demo["tokens"]["engineer"])).status_code == 201

    names = {i["dataset_name"] for i in overview(client, demo).json()["items"]}

    assert names == {"raw_customers", "raw_orders", "raw_payments"}


def test_rule_changes_log_ids_only(client, demo, caplog):
    caplog.set_level(logging.INFO, logger="dataharbor_api.freshness")
    pipeline = pipeline_id(client, demo)

    put_rule(client, demo, signal="pipeline_run", pipeline_id=pipeline)

    messages = [r.getMessage() for r in caplog.records]
    assert any("freshness rule saved" in m for m in messages)
    assert not any("@" in m for m in messages)
