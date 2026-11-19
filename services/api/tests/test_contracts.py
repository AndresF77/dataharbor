import sys
from pathlib import Path

import pytest

from tests.helpers import add_user, auth

REPO_ROOT = Path(__file__).resolve().parents[3]

ORDERS_CONTRACT = {
    "format": "csv",
    "columns": [
        {"name": "order_id", "type": "string"},
        {"name": "amount", "type": "decimal"},
    ],
    "primary_key": ["order_id"],
}


def base(demo) -> str:
    return f"/v1/workspaces/{demo['workspace_id']}/datasets"


def dataset_id(client, demo, name: str) -> str:
    items = client.get(base(demo), headers=auth(demo["tokens"]["viewer"])).json()["items"]
    return next(d["id"] for d in items if d["name"] == name)


def new_raw_dataset(client, demo, name: str = "raw_refunds") -> str:
    response = client.post(
        base(demo),
        json={"name": name, "layer": "raw"},
        headers=auth(demo["tokens"]["engineer"]),
    )
    assert response.status_code == 201
    return response.json()["id"]


def contract_url(demo, ds_id: str, suffix: str = "") -> str:
    return f"{base(demo)}/{ds_id}/contract{suffix}"


def publish(client, demo, ds_id, body, role="engineer"):
    return client.post(contract_url(demo, ds_id), json=body, headers=auth(demo["tokens"][role]))


def test_seed_publishes_contracts_for_raw_datasets(client, demo):
    viewer = auth(demo["tokens"]["viewer"])
    orders = client.get(contract_url(demo, dataset_id(client, demo, "raw_orders")), headers=viewer)
    assert orders.status_code == 200
    body = orders.json()
    assert body["version"] == 1
    assert body["format"] == "csv"
    assert body["primary_key"] == ["order_id"]
    assert {"name": "amount", "type": "decimal", "nullable": False} in body["columns"]

    payments = client.get(
        contract_url(demo, dataset_id(client, demo, "raw_payments")), headers=viewer
    )
    assert payments.json()["format"] == "jsonl"

    staging = client.get(
        contract_url(demo, dataset_id(client, demo, "stg_orders")), headers=viewer
    )
    assert staging.status_code == 404
    assert staging.json()["error"]["message"] == "No ingestion contract yet"


def test_publish_first_and_next_versions(client, demo):
    ds_id = new_raw_dataset(client, demo)

    first = publish(client, demo, ds_id, {**ORDERS_CONTRACT, "based_on_version": None})
    assert first.status_code == 201
    assert first.json()["version"] == 1

    changed = {**ORDERS_CONTRACT, "columns": [*ORDERS_CONTRACT["columns"]]}
    changed["columns"].append({"name": "note", "type": "string", "nullable": True})
    second = publish(client, demo, ds_id, {**changed, "based_on_version": 1})
    assert second.status_code == 201
    assert second.json()["version"] == 2

    viewer = auth(demo["tokens"]["viewer"])
    latest = client.get(contract_url(demo, ds_id), headers=viewer).json()
    assert latest["version"] == 2
    versions = client.get(contract_url(demo, ds_id, "/versions"), headers=viewer).json()
    assert [v["version"] for v in versions["items"]] == [2, 1]
    old = client.get(contract_url(demo, ds_id, "/versions/1"), headers=viewer).json()
    assert len(old["columns"]) == 2
    missing = client.get(contract_url(demo, ds_id, "/versions/9"), headers=viewer)
    assert missing.status_code == 404


@pytest.mark.parametrize("based_on", [None, 2])
def test_publish_from_stale_version_conflicts(client, demo, based_on):
    ds_id = new_raw_dataset(client, demo)
    publish(client, demo, ds_id, {**ORDERS_CONTRACT, "based_on_version": None})

    response = publish(client, demo, ds_id, {**ORDERS_CONTRACT, "based_on_version": based_on})
    assert response.status_code == 409
    assert "latest version 1" in response.json()["error"]["message"]


def test_first_publish_must_not_claim_a_base(client, demo):
    ds_id = new_raw_dataset(client, demo)
    response = publish(client, demo, ds_id, {**ORDERS_CONTRACT, "based_on_version": 1})
    assert response.status_code == 409
    assert "latest version none" in response.json()["error"]["message"]


def test_publish_requires_based_on_version(client, demo):
    ds_id = new_raw_dataset(client, demo)
    assert publish(client, demo, ds_id, ORDERS_CONTRACT).status_code == 422


def test_only_raw_datasets_take_contracts(client, demo):
    ds_id = dataset_id(client, demo, "stg_orders")
    response = publish(client, demo, ds_id, {**ORDERS_CONTRACT, "based_on_version": None})
    assert response.status_code == 422
    assert response.json()["error"]["message"] == "Ingestion contracts apply to raw datasets only"


def test_invalid_contract_is_rejected(client, demo):
    ds_id = new_raw_dataset(client, demo)
    body = {**ORDERS_CONTRACT, "primary_key": ["missing"], "based_on_version": None}
    response = publish(client, demo, ds_id, body)
    assert response.status_code == 422
    assert response.json()["error"]["details"][0]["msg"].endswith("is not in columns")


def test_role_and_workspace_checks(client, demo):
    ds_id = dataset_id(client, demo, "raw_orders")
    body = {**ORDERS_CONTRACT, "based_on_version": 1}

    assert publish(client, demo, ds_id, body, role="viewer").status_code == 403
    assert client.get(contract_url(demo, ds_id)).status_code == 401

    _, outsider = add_user(client, "olive.outsider@example.test")
    assert client.get(contract_url(demo, ds_id), headers=auth(outsider)).status_code == 404
    validate = client.post(
        contract_url(demo, ds_id, "/validate"), content=b"order_id\n", headers=auth(outsider)
    )
    assert validate.status_code == 404


def test_validate_sample_against_latest_and_older_versions(client, demo):
    ds_id = new_raw_dataset(client, demo)
    publish(client, demo, ds_id, {**ORDERS_CONTRACT, "based_on_version": None})
    v2 = {**ORDERS_CONTRACT, "columns": [*ORDERS_CONTRACT["columns"]]}
    v2["columns"].append({"name": "currency", "type": "string"})
    publish(client, demo, ds_id, {**v2, "based_on_version": 1})

    sample = b"order_id,amount\nORD-1,10.00\nORD-1,\n"
    headers = {**auth(demo["tokens"]["viewer"]), "Content-Type": "text/csv"}

    latest = client.post(contract_url(demo, ds_id, "/validate"), content=sample, headers=headers)
    assert latest.status_code == 200
    body = latest.json()
    assert body["valid"] is False
    assert body["contract_version"] == 2
    assert body["record_count"] == 2
    assert [(v["rule"], v["column"], v["record"]) for v in body["violations"]] == [
        ("missing_column", "currency", None),
        ("null_value", "amount", 2),
        ("duplicate_key", "order_id", 2),
    ]

    older = client.post(
        contract_url(demo, ds_id, "/validate?version=1"), content=sample, headers=headers
    ).json()
    assert older["contract_version"] == 1
    assert [v["rule"] for v in older["violations"]] == ["null_value", "duplicate_key"]


def test_validate_rejects_empty_and_oversized_samples(client, demo, monkeypatch):
    from dataharbor_api import contracts

    ds_id = dataset_id(client, demo, "raw_orders")
    url = contract_url(demo, ds_id, "/validate")
    headers = auth(demo["tokens"]["viewer"])

    empty = client.post(url, content=b"", headers=headers)
    assert empty.status_code == 422

    monkeypatch.setattr(contracts, "MAX_SAMPLE_BYTES", 16)
    too_big = client.post(url, content=b"x" * 17, headers=headers)
    assert too_big.status_code == 413
    assert too_big.json()["error"]["code"] == "payload_too_large"


def test_validate_without_contract_is_404(client, demo):
    ds_id = new_raw_dataset(client, demo)
    response = client.post(
        contract_url(demo, ds_id, "/validate"),
        content=b"order_id\n",
        headers=auth(demo["tokens"]["viewer"]),
    )
    assert response.status_code == 404


@pytest.fixture
def fixtures_module():
    path = str(REPO_ROOT / "packages" / "fixtures")
    sys.path.insert(0, path)
    try:
        yield pytest.importorskip("dataharbor_fixtures.generator")
    finally:
        sys.path.remove(path)


def validate_file(client, demo, dataset: str, data: bytes) -> dict:
    response = client.post(
        contract_url(demo, dataset_id(client, demo, dataset), "/validate"),
        content=data,
        headers=auth(demo["tokens"]["viewer"]),
    )
    assert response.status_code == 200
    return response.json()


SOURCE_DATASETS = {"customers": "raw_customers", "orders": "raw_orders", "payments": "raw_payments"}


def test_clean_fixtures_match_seeded_contracts(client, demo, fixtures_module):
    fixture_set = fixtures_module.generate(fixtures_module.Options(days=2))
    for entry in fixture_set.manifest["files"]:
        report = validate_file(
            client, demo, SOURCE_DATASETS[entry["source"]], fixture_set.files[entry["path"]]
        )
        assert report["valid"], (entry["path"], report["violations"])
        assert report["record_count"] == entry["record_count"]


def test_fixture_defects_are_caught_by_contracts(client, demo, fixtures_module):
    fixture_set = fixtures_module.generate(fixtures_module.Options(defects=True))
    expected = {
        "duplicate_order_id": "duplicate_key",
        "null_payment_amount": "null_value",
        "renamed_column": "missing_column",
    }
    for defect in fixture_set.manifest["defects"]:
        if defect["type"] not in expected:
            continue  # orphan payments and late files are checked after landing
        source = "payments" if defect["file"].startswith("payments/") else "orders"
        report = validate_file(
            client, demo, SOURCE_DATASETS[source], fixture_set.files[defect["file"]]
        )
        assert not report["valid"]
        assert expected[defect["type"]] in {v["rule"] for v in report["violations"]}
