import logging
import uuid

import pytest
from pydantic import ValidationError

from dataharbor_api.contract_rules import ContractSpec
from dataharbor_api.models import QualityRule
from dataharbor_api.quality_checks import MAX_SAMPLE_RECORDS, CheckSpec, run_checks
from dataharbor_api.seed import DEMO_CONTRACTS
from tests.helpers import add_user, auth
from tests.test_landing import ORDERS_HEADER, base, dataset_id, land, store

ORDERS = ContractSpec(**DEMO_CONTRACTS["raw_orders"])
PAYMENTS = ContractSpec(**DEMO_CONTRACTS["raw_payments"])


def order_line(n: int, *, quantity: str = "2", amount: str = "9.00", status: str = "completed",
               customer: str | None = None) -> str:
    customer = f"c-{n}" if customer is None else customer
    return (f"o-{n},{customer},Harbor Goods,Cedar Mug,{quantity},4.50,{amount},USD,{status},"
            f"2026-03-01T10:00:00Z\n")


def orders_csv(*lines: str) -> bytes:
    return (ORDERS_HEADER + "".join(lines)).encode()


CLEAN = orders_csv(order_line(1), order_line(2, status="cancelled"))
# record 2: unknown status (error rule); record 3: 150 items (warn rule)
DEFECTIVE = orders_csv(
    order_line(1), order_line(2, status="refunded"), order_line(3, quantity="150")
)
ONLY_WARNINGS = orders_csv(order_line(1), order_line(2, quantity="150"))


def run(spec: dict, data: bytes, contract: ContractSpec = ORDERS):
    count, (outcome,) = run_checks(contract, data, [CheckSpec(**spec)])
    return count, outcome


def rules_url(client, demo, name="raw_orders", suffix=""):
    return f"{base(demo)}/{dataset_id(client, demo, name)}/quality-rules{suffix}"


def rules(client, demo, name="raw_orders") -> dict[str, dict]:
    response = client.get(rules_url(client, demo, name), headers=auth(demo["tokens"]["viewer"]))
    assert response.status_code == 200
    return {r["name"]: r for r in response.json()["items"]}


def create_rule(client, demo, dataset="raw_orders", role="engineer", **body):
    return client.post(rules_url(client, demo, dataset), json=body,
                       headers=auth(demo["tokens"][role]))


def check(client, demo, file_id, name="raw_orders", role="engineer"):
    return client.post(
        f"{base(demo)}/{dataset_id(client, demo, name)}/files/{file_id}/quality-checks",
        headers=auth(demo["tokens"][role]),
    )


def landed(client, demo, data, business_date="2026-03-01") -> str:
    response = land(client, demo, data=data, business_date=business_date)
    assert response.status_code == 201, response.text
    return response.json()["id"]


def by_rule(run_body) -> dict[str, dict]:
    return {r["rule_name"]: r for r in run_body["results"]}


# --- check definitions ---------------------------------------------------------------


@pytest.mark.parametrize(
    "spec",
    [
        {"check": "not_null", "columns": []},
        {"check": "not_null", "columns": ["a", "b"]},
        {"check": "unique", "columns": []},
        {"check": "unique", "columns": ["a", "a"]},
        {"check": "accepted_values", "columns": ["a"]},
        {"check": "accepted_values", "columns": ["a"], "values": []},
        {"check": "accepted_values", "columns": ["a"], "values": ["x", "x"]},
        {"check": "accepted_values", "columns": ["a"], "values": ["x" * 201]},
        {"check": "not_null", "columns": ["a"], "values": ["x"]},
        {"check": "value_range", "columns": ["a"]},
        {"check": "not_null", "columns": ["a"], "min": "1"},
        {"check": "row_count", "min": "1", "columns": ["a"]},
        {"check": "row_count"},
        {"check": "row_count", "min": "-1"},
        {"check": "row_count", "min": "1.5"},
        {"check": "row_count", "min": "5", "max": "4"},
        {"check": "not_null", "columns": ["Bad Name"]},
        {"check": "not_null", "columns": ["a"], "regex": ".*"},
        {"check": "freshness", "columns": ["a"]},
    ],
)
def test_malformed_check_definitions_are_rejected(spec):
    with pytest.raises(ValidationError):
        CheckSpec(**spec)


@pytest.mark.parametrize(
    ("spec", "problem"),
    [
        ({"check": "not_null", "columns": ["channel"]}, "channel is not a column of the contract"),
        ({"check": "value_range", "columns": ["status"], "min": "a"},
         "status is string"),
        ({"check": "value_range", "columns": ["amount"], "min": "ten"},
         "min is not a valid decimal"),
        ({"check": "value_range", "columns": ["quantity"], "max": "1.5"},
         "max is not a valid integer"),
        ({"check": "value_range", "columns": ["amount"], "min": "10", "max": "9.99"},
         "min must not be above max"),
        ({"check": "value_range", "columns": ["ordered_at"], "min": "2026-03-01T00:00:00"},
         "min is not a valid timestamp"),  # timestamps need an offset
    ],
)
def test_checks_that_cannot_run_on_the_contract_say_why(spec, problem):
    problems = CheckSpec(**spec).problems_with(ORDERS)

    assert any(problem in p for p in problems), problems


def test_valid_range_bounds_have_no_problems():
    spec = CheckSpec(check="value_range", columns=["ordered_at"],
                     min="2026-01-01T00:00:00Z", max="2026-12-31T23:59:59+00:00")

    assert spec.problems_with(ORDERS) == []


# --- the engine ----------------------------------------------------------------------


def test_not_null_counts_empty_values_with_record_numbers():
    data = orders_csv(order_line(1), order_line(2, customer=""), order_line(3))
    # customer_id is a required string in the contract, but the engine judges the file itself
    count, outcome = run({"check": "not_null", "columns": ["customer_id"]}, data)

    assert count == 3
    assert not outcome.passed
    assert (outcome.checked_records, outcome.failing_records) == (3, 1)
    assert outcome.sample_records == [2]
    assert outcome.message == "1 of 3 checked record failed"


def test_unique_reports_repeats_after_the_first_and_skips_nulls():
    data = orders_csv(
        order_line(1, customer="c-1"), order_line(2, customer="c-1"),
        order_line(3, customer=""), order_line(4, customer=""), order_line(5, customer="c-1"),
    )
    _, outcome = run({"check": "unique", "columns": ["customer_id"]}, data)

    assert outcome.checked_records == 3
    assert outcome.sample_records == [2, 5]


def test_unique_over_several_columns():
    data = orders_csv(
        order_line(1, customer="c-1"), order_line(2, customer="c-1", status="cancelled"),
        order_line(3, customer="c-1"),
    )
    _, outcome = run({"check": "unique", "columns": ["customer_id", "status"]}, data)

    assert outcome.sample_records == [3]


def test_accepted_values_are_exact_and_case_sensitive():
    data = orders_csv(order_line(1), order_line(2, status="Completed"), order_line(3, status=""))
    _, outcome = run(
        {"check": "accepted_values", "columns": ["status"], "values": ["completed"]}, data
    )

    assert (outcome.checked_records, outcome.sample_records) == (2, [2])


@pytest.mark.parametrize(
    ("bounds", "failing"),
    [
        ({"min": "9.00"}, [2]),  # inclusive lower bound: 9.00 passes, 8.99 fails
        ({"max": "9.00"}, [3]),
        ({"min": "9", "max": "9.01"}, [2]),  # 9.010 equals 9.01
        ({"min": "0"}, []),
    ],
)
def test_decimal_ranges_are_inclusive_and_exact(bounds, failing):
    data = orders_csv(order_line(1, amount="9.00"), order_line(2, amount="8.99"),
                      order_line(3, amount="9.010"))
    _, outcome = run({"check": "value_range", "columns": ["amount"], **bounds}, data)

    assert outcome.sample_records == failing
    assert outcome.passed is (not failing)


def test_timestamp_range_compares_instants_across_offsets():
    data = orders_csv(order_line(1)).replace(b"2026-03-01T10:00:00Z", b"2026-03-01T11:00:00+02:00")
    _, outcome = run({"check": "value_range", "columns": ["ordered_at"],
                      "min": "2026-03-01T09:30:00Z"}, data)

    assert outcome.sample_records == [1]  # 09:00 UTC is before 09:30 UTC


def test_jsonl_values_compare_by_type():
    lines = [
        '{"payment_id": "p-1", "order_id": "o-1", "method": "card", "amount": 12.5, '
        '"currency": "USD", "status": "captured", "paid_at": "2026-03-01T10:00:00Z"}',
        '{"payment_id": "p-2", "order_id": "o-2", "method": "cash", "amount": "0.00", '
        '"currency": "USD", "status": "captured", "paid_at": "2026-03-01T10:00:00Z"}',
        "",
        '{"payment_id": "p-3", "order_id": "o-3", "method": "card", "amount": null, '
        '"currency": "USD", "status": "captured", "paid_at": "2026-03-01T10:00:00Z"}',
    ]
    data = "\n".join(lines).encode()

    count, outcomes = run_checks(PAYMENTS, data, [
        CheckSpec(check="value_range", columns=["amount"], min="0.01"),
        CheckSpec(check="accepted_values", columns=["method"], values=["card", "wallet"]),
        CheckSpec(check="not_null", columns=["amount"]),
    ])

    assert count == 3
    assert [o.sample_records for o in outcomes] == [[2], [2], [3]]


def test_row_count_judges_the_whole_file():
    _, low = run({"check": "row_count", "min": "3"}, CLEAN)
    _, ok = run({"check": "row_count", "min": "1", "max": "2"}, CLEAN)
    _, empty = run({"check": "row_count", "max": "0"}, orders_csv())

    assert not low.passed
    assert low.message == "2 records; expected at least 3"
    assert low.sample_records == []
    assert ok.passed
    assert ok.message == "2 records; expected at least 1 and at most 2"
    assert empty.passed


def test_samples_are_capped_but_counts_are_not():
    data = orders_csv(*(order_line(n, status="refunded") for n in range(1, 31)))
    _, outcome = run(
        {"check": "accepted_values", "columns": ["status"], "values": ["completed"]}, data
    )

    assert outcome.failing_records == 30
    assert outcome.sample_records == list(range(1, MAX_SAMPLE_RECORDS + 1))


def test_outcomes_never_contain_values():
    data = orders_csv(order_line(1, status="secret-status-value"))
    _, outcome = run(
        {"check": "accepted_values", "columns": ["status"], "values": ["completed"]}, data
    )

    assert "secret-status-value" not in repr(outcome)


# --- rules API -----------------------------------------------------------------------


def test_seed_adds_quality_rules_to_raw_datasets(client, demo):
    assert set(rules(client, demo, "raw_customers")) == {
        "has_customers", "email_unique", "segment_known",
    }
    orders = rules(client, demo)
    assert set(orders) == {"amount_non_negative", "quantity_plausible", "status_known"}
    assert orders["quantity_plausible"]["check"] == "value_range"
    quantity = orders["quantity_plausible"]
    assert (quantity["min"], quantity["max"]) == ("1", "100")
    assert orders["quantity_plausible"]["severity"] == "warn"
    assert orders["status_known"]["values"] == ["completed", "cancelled"]
    assert set(rules(client, demo, "raw_payments")) == {
        "amount_positive", "method_known", "currency_usd",
    }


def test_seeding_twice_adds_no_rules(client, demo):
    from dataharbor_api.seed import seed_demo

    with client.app.state.database.new_session() as session:
        seed_demo(session)
        total = session.query(QualityRule).count()

    assert total == 9


def test_create_and_read_a_rule(client, demo):
    response = create_rule(
        client, demo, name="merchant_present", check="not_null", columns=["merchant"],
        severity="warn", description="Every order names its merchant",
    )

    assert response.status_code == 201
    body = response.json()
    assert body["version"] == 1
    assert body["enabled"] is True
    assert (body["values"], body["min"], body["max"]) == (None, None, None)
    detail = client.get(rules_url(client, demo, suffix=f"/{body['id']}"),
                        headers=auth(demo["tokens"]["viewer"]))
    assert detail.json() == body


@pytest.mark.parametrize(
    ("body", "fragment"),
    [
        ({"check": "not_null", "columns": ["channel"]}, "channel is not a column"),
        ({"check": "value_range", "columns": ["merchant"], "min": "a"}, "merchant is string"),
        ({"check": "value_range", "columns": ["amount"], "min": "5", "max": "1"},
         "min must not be above max"),
    ],
)
def test_rules_must_fit_the_latest_contract(client, demo, body, fragment):
    response = create_rule(client, demo, name="bad_rule", **body)

    assert response.status_code == 422
    assert fragment in response.json()["error"]["message"]
    assert "bad_rule" not in rules(client, demo)


@pytest.mark.parametrize(
    "body",
    [
        {"name": "x", "check": "not_null", "columns": ["merchant"]},  # name too short
        {"name": "Bad-Name", "check": "not_null", "columns": ["merchant"]},
        {"name": "ok_name", "check": "not_null", "columns": ["merchant"], "severity": "fatal"},
        {"name": "ok_name", "check": "row_count"},
        {"name": "ok_name", "check": "not_null", "columns": ["merchant"], "description": "d" * 501},
    ],
)
def test_malformed_rules_are_rejected(client, demo, body):
    response = client.post(rules_url(client, demo), json=body,
                           headers=auth(demo["tokens"]["engineer"]))

    assert response.status_code == 422


def test_only_raw_datasets_with_a_contract_take_rules(client, demo):
    staging = client.post(rules_url(client, demo, "stg_orders"),
                          json={"name": "has_rows", "check": "row_count", "min": "1"},
                          headers=auth(demo["tokens"]["engineer"]))
    assert staging.status_code == 422
    assert "only raw datasets" in staging.json()["error"]["message"]

    created = client.post(
        base(demo), json={"name": "raw_refunds", "layer": "raw"},
        headers=auth(demo["tokens"]["engineer"]),
    )
    assert created.status_code == 201
    no_contract = client.post(
        f"{base(demo)}/{created.json()['id']}/quality-rules",
        json={"name": "has_rows", "check": "row_count", "min": "1"},
        headers=auth(demo["tokens"]["engineer"]),
    )
    assert no_contract.status_code == 422
    assert "Publish an ingestion contract" in no_contract.json()["error"]["message"]


def test_rule_names_are_unique_per_dataset(client, demo):
    response = create_rule(client, demo, name="status_known", check="not_null",
                           columns=["status"])

    assert response.status_code == 409
    other = client.post(rules_url(client, demo, "raw_customers"),
                        json={"name": "status_known", "check": "row_count", "min": "1"},
                        headers=auth(demo["tokens"]["engineer"]))
    assert other.status_code == 201


def test_replace_needs_the_current_version(client, demo):
    rule = rules(client, demo)["quantity_plausible"]
    url = rules_url(client, demo, suffix=f"/{rule['id']}")
    engineer = auth(demo["tokens"]["engineer"])
    body = {"check": "value_range", "columns": ["quantity"], "min": "1", "max": "50",
            "severity": "error", "version": 1}

    first = client.put(url, json=body, headers=engineer)
    stale = client.put(url, json={**body, "max": "60"}, headers=engineer)

    assert first.status_code == 200
    assert (first.json()["version"], first.json()["max"], first.json()["severity"]) == (
        2, "50", "error"
    )
    assert stale.status_code == 409
    assert "current version 2" in stale.json()["error"]["message"]
    assert client.get(url, headers=engineer).json()["max"] == "50"


def test_roles_and_workspace_scoping(client, demo):
    _, outsider = add_user(client, "outsider@example.test")
    rule = rules(client, demo)["status_known"]
    url = rules_url(client, demo, suffix=f"/{rule['id']}")

    assert create_rule(client, demo, role="viewer", name="has_rows", check="row_count",
                       min="1").status_code == 403
    assert client.delete(url, headers=auth(demo["tokens"]["viewer"])).status_code == 403
    assert client.get(url, headers=auth(outsider)).status_code == 404
    assert client.get(rules_url(client, demo)).status_code == 401
    other_dataset = rules_url(client, demo, "raw_payments", suffix=f"/{rule['id']}")
    assert client.get(other_dataset,
                      headers=auth(demo["tokens"]["viewer"])).status_code == 404


# --- running checks ------------------------------------------------------------------


def test_clean_file_passes_and_reruns_return_the_recorded_results(client, demo):
    file_id = landed(client, demo, CLEAN)

    first = check(client, demo, file_id)
    again = check(client, demo, file_id)

    assert first.status_code == 201
    body = first.json()
    assert body["status"] == "passed"
    assert body["record_count"] == 2
    assert body["contract_version"] == 1
    assert body["counts"] == {"pass": 3, "fail": 0, "error": 0}
    assert [r["rule_name"] for r in body["results"]] == [
        "amount_non_negative", "quantity_plausible", "status_known",
    ]
    assert again.status_code == 200
    assert again.json() == body


def test_failing_error_rule_fails_the_file_without_echoing_values(client, demo, caplog):
    file_id = landed(client, demo, DEFECTIVE)

    with caplog.at_level(logging.INFO, logger="dataharbor_api"):
        response = check(client, demo, file_id)

    body = response.json()
    assert body["status"] == "failed"
    assert body["counts"] == {"pass": 1, "fail": 2, "error": 0}
    results = by_rule(body)
    status_known = results["status_known"]
    assert (status_known["status"], status_known["severity"]) == ("fail", "error")
    assert (status_known["checked_records"], status_known["failing_records"]) == (3, 1)
    assert status_known["sample_records"] == [2]
    assert status_known["message"] == "1 of 3 checked record failed"
    assert results["quantity_plausible"]["sample_records"] == [3]
    assert results["amount_non_negative"]["status"] == "pass"
    assert "refunded" not in response.text
    assert "refunded" not in caplog.text
    assert "status=failed" in caplog.text


def test_only_warn_failures_make_a_warning(client, demo):
    response = check(client, demo, landed(client, demo, ONLY_WARNINGS))

    assert response.json()["status"] == "warning"


def test_no_enabled_rules_means_no_rules(client, demo):
    engineer = auth(demo["tokens"]["engineer"])
    for rule in rules(client, demo).values():
        body = {k: rule[k] for k in ("check", "columns", "values", "min", "max", "severity")}
        client.put(rules_url(client, demo, suffix=f"/{rule['id']}"),
                   json={**body, "enabled": False, "version": rule["version"]}, headers=engineer)

    response = check(client, demo, landed(client, demo, DEFECTIVE))

    assert response.json()["status"] == "no_rules"
    assert response.json()["results"] == []


def test_changed_rule_is_evaluated_again_as_a_new_version(client, demo):
    file_id = landed(client, demo, DEFECTIVE)
    assert check(client, demo, file_id).json()["status"] == "failed"
    rule = rules(client, demo)["status_known"]
    client.put(
        rules_url(client, demo, suffix=f"/{rule['id']}"),
        json={"check": "accepted_values", "columns": ["status"],
              "values": ["completed", "cancelled", "refunded"], "version": 1},
        headers=auth(demo["tokens"]["engineer"]),
    )

    rerun = check(client, demo, file_id)

    assert rerun.status_code == 201
    assert by_rule(rerun.json())["status_known"]["rule_version"] == 2
    assert rerun.json()["status"] == "warning"  # quantity_plausible still fails (warn)
    history = client.get(
        f"{base(demo)}/{dataset_id(client, demo, 'raw_orders')}/quality-results",
        params={"rule_id": rule["id"]}, headers=auth(demo["tokens"]["viewer"]),
    ).json()["items"]
    assert sorted((r["rule_version"], r["status"]) for r in history) == [(1, "fail"), (2, "pass")]


def test_rule_that_does_not_fit_the_files_contract_records_an_error(client, demo):
    file_id = landed(client, demo, CLEAN)
    orders_id = dataset_id(client, demo, "raw_orders")
    engineer = auth(demo["tokens"]["engineer"])
    contract = {**DEMO_CONTRACTS["raw_orders"]}
    contract["columns"] = [*contract["columns"],
                           {"name": "channel", "type": "string", "nullable": True}]
    published = client.post(f"{base(demo)}/{orders_id}/contract",
                            json={**contract, "based_on_version": 1}, headers=engineer)
    assert published.status_code == 201
    assert create_rule(client, demo, name="channel_present", check="not_null",
                       columns=["channel"], severity="warn").status_code == 201

    body = check(client, demo, file_id).json()

    result = by_rule(body)["channel_present"]
    assert result["status"] == "error"
    assert result["message"] == (
        "Cannot run on contract version 1: channel is not a column of the contract"
    )
    assert body["counts"]["error"] == 1
    assert body["status"] == "warning"  # an erroring warn rule only warns


def test_tampered_file_is_refused(client, demo):
    file_id = landed(client, demo, CLEAN)
    key = next(iter(store(client).objects))
    store(client).objects[key] = (DEFECTIVE, "text/csv")

    response = check(client, demo, file_id)

    assert response.status_code == 409
    assert "checksum" in response.json()["error"]["message"]
    results = client.get(f"/v1/workspaces/{demo['workspace_id']}/quality-results",
                         headers=auth(demo["tokens"]["viewer"])).json()["items"]
    assert results == []


def test_storage_outage_is_503_and_records_nothing(client, demo):
    file_id = landed(client, demo, CLEAN)
    store(client).fail = True
    try:
        response = check(client, demo, file_id)
    finally:
        store(client).fail = False

    assert response.status_code == 503
    assert check(client, demo, file_id).status_code == 201


def test_running_checks_needs_an_engineer_and_a_file_of_the_dataset(client, demo):
    file_id = landed(client, demo, CLEAN)

    assert check(client, demo, file_id, role="viewer").status_code == 403
    assert check(client, demo, str(uuid.uuid4())).status_code == 404
    assert check(client, demo, file_id, name="raw_payments").status_code == 404


def test_deleting_a_rule_keeps_its_results(client, demo):
    file_id = landed(client, demo, DEFECTIVE)
    check(client, demo, file_id)
    rule = rules(client, demo)["status_known"]

    deleted = client.delete(rules_url(client, demo, suffix=f"/{rule['id']}"),
                            headers=auth(demo["tokens"]["engineer"]))

    assert deleted.status_code == 204
    history = client.get(f"/v1/workspaces/{demo['workspace_id']}/quality-results",
                         params={"status": "fail", "severity": "error"},
                         headers=auth(demo["tokens"]["viewer"])).json()["items"]
    assert [(r["rule_name"], r["rule_id"]) for r in history] == [("status_known", None)]
    rerun = check(client, demo, file_id).json()
    assert "status_known" not in by_rule(rerun)


# --- result history ------------------------------------------------------------------


def test_history_filters_and_workspace_scoping(client, demo):
    clean_id = landed(client, demo, CLEAN, business_date="2026-03-01")
    bad_id = landed(client, demo, DEFECTIVE, business_date="2026-03-02")
    check(client, demo, clean_id)
    check(client, demo, bad_id)
    viewer = auth(demo["tokens"]["viewer"])
    url = f"{base(demo)}/{dataset_id(client, demo, 'raw_orders')}/quality-results"

    def names(**params):
        response = client.get(url, params=params, headers=viewer)
        assert response.status_code == 200
        return sorted(r["rule_name"] for r in response.json()["items"])

    assert len(names()) == 6
    assert names(status=["fail", "error"]) == ["quantity_plausible", "status_known"]
    assert names(status="fail", severity="warn") == ["quantity_plausible"]
    assert names(landed_file_id=clean_id) == [
        "amount_non_negative", "quantity_plausible", "status_known",
    ]
    assert names(business_date="2026-03-02", status="pass") == ["amount_non_negative"]
    assert len(names(limit=2)) == 2
    assert client.get(url, params={"limit": 0}, headers=viewer).status_code == 422
    assert client.get(url, params={"status": "broken"}, headers=viewer).status_code == 422

    _, outsider = add_user(client, "outsider@example.test")
    created = client.post("/v1/workspaces", json={"slug": "other-co", "name": "Other"},
                          headers=auth(outsider))
    assert created.status_code == 201
    theirs = client.get(f"/v1/workspaces/{created.json()['id']}/quality-results",
                        params={"dataset_id": dataset_id(client, demo, "raw_orders")},
                        headers=auth(outsider))
    assert theirs.status_code == 200
    assert theirs.json()["items"] == []
    assert client.get(url, headers=auth(outsider)).status_code == 404


def test_file_status_from_results():
    from types import SimpleNamespace

    from dataharbor_api.models import ResultStatus, Severity
    from dataharbor_api.quality import FileQuality, file_quality

    def result(status, severity):
        return SimpleNamespace(status=ResultStatus(status), severity=Severity(severity))

    assert file_quality([]) is FileQuality.NO_RULES
    assert file_quality([result("pass", "error")]) is FileQuality.PASSED
    assert file_quality([result("pass", "error"), result("error", "warn")]) is FileQuality.WARNING
    assert file_quality([result("fail", "warn"), result("error", "error")]) is FileQuality.FAILED
