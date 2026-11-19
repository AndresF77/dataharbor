import hashlib
import logging

import pytest

from dataharbor_api.storage import MemoryObjectStore, StorageError
from tests.helpers import add_user, auth

ORDERS_HEADER = (
    "order_id,customer_id,merchant,product,quantity,unit_price,amount,currency,status,ordered_at\n"
)
ORDERS_CSV = (
    ORDERS_HEADER
    + "o-1,c-1,Harbor Goods,Cedar Mug,2,4.50,9.00,USD,paid,2026-03-01T10:00:00Z\n"
    + "o-2,c-2,Willow Market,Fern Lamp,1,20.00,20.00,USD,paid,2026-03-01T11:30:00Z\n"
).encode()
DATE = "2026-03-01"


def base(demo) -> str:
    return f"/v1/workspaces/{demo['workspace_id']}/datasets"


def dataset_id(client, demo, name: str) -> str:
    items = client.get(base(demo), headers=auth(demo["tokens"]["viewer"])).json()["items"]
    return next(d["id"] for d in items if d["name"] == name)


def files_url(client, demo, name: str = "raw_orders", suffix: str = "") -> str:
    return f"{base(demo)}/{dataset_id(client, demo, name)}/files{suffix}"


def land(client, demo, data=ORDERS_CSV, name="raw_orders", role="engineer",
         business_date=DATE, filename="orders_2026-03-01.csv", headers=None):
    return client.post(
        files_url(client, demo, name),
        params={"business_date": business_date, "filename": filename},
        content=data,
        headers={**auth(demo["tokens"][role]), **(headers or {})},
    )


def store(client) -> MemoryObjectStore:
    return client.app.state.object_store


def test_land_valid_file_stores_object_with_checksum(client, demo):
    response = land(client, demo)

    assert response.status_code == 201
    body = response.json()
    digest = hashlib.sha256(ORDERS_CSV).hexdigest()
    assert body["sha256"] == digest
    assert body["size_bytes"] == len(ORDERS_CSV)
    assert body["record_count"] == 2
    assert body["contract_version"] == 1
    assert body["business_date"] == DATE
    assert body["original_filename"] == "orders_2026-03-01.csv"
    assert body["duplicate"] is False
    assert body["object_key"] == (
        f"workspaces/{demo['workspace_id']}/raw_orders/business_date={DATE}/{digest}.csv"
    )
    data, content_type = store(client).objects[body["object_key"]]
    assert data == ORDERS_CSV
    assert content_type == "text/csv"


def test_same_bytes_land_once(client, demo):
    first = land(client, demo)
    again = land(client, demo, filename="orders_copy.csv", business_date="2026-03-02")

    assert again.status_code == 200
    assert again.json()["duplicate"] is True
    assert again.json()["id"] == first.json()["id"]
    assert again.json()["original_filename"] == "orders_2026-03-01.csv"
    assert len(store(client).objects) == 1


def test_contract_violations_are_reported_and_nothing_is_stored(client, demo):
    duplicate = ORDERS_CSV + b"o-1,c-3,Harbor Goods,Cedar Mug,1,4.50,4.50,USD,paid,2026-03-01T12:00:00Z\n"

    response = land(client, demo, data=duplicate)

    assert response.status_code == 422
    error = response.json()["error"]
    assert error["message"] == "File does not match the ingestion contract"
    assert error["details"]["violation_count"] == 1
    assert error["details"]["violations"][0]["rule"] == "duplicate_key"
    assert "o-1" not in response.text  # source values are never echoed
    assert store(client).objects == {}
    listed = client.get(files_url(client, demo), headers=auth(demo["tokens"]["viewer"]))
    assert listed.json()["items"] == []


def test_declared_checksum_must_match_body(client, demo):
    digest = hashlib.sha256(ORDERS_CSV).hexdigest()

    ok = land(client, demo, headers={"X-Content-SHA256": digest.upper()})
    assert ok.status_code == 201

    other = ORDERS_CSV.replace(b"9.00", b"9.01").replace(b",2,4.50", b",2,4.505")
    wrong = land(client, demo, data=other, headers={"X-Content-SHA256": digest})
    assert wrong.status_code == 422
    assert wrong.json()["error"]["message"] == "Body does not match X-Content-SHA256"

    malformed = land(client, demo, data=other, headers={"X-Content-SHA256": "abc"})
    assert malformed.status_code == 422
    assert len(store(client).objects) == 1


def test_landing_requires_engineer_and_membership(client, demo):
    assert land(client, demo, role="viewer").status_code == 403

    _, outsider = add_user(client, "outsider@example.test")
    response = client.post(
        files_url(client, demo),
        params={"business_date": DATE, "filename": "orders.csv"},
        content=ORDERS_CSV,
        headers=auth(outsider),
    )
    assert response.status_code == 404

    anonymous = client.post(
        files_url(client, demo),
        params={"business_date": DATE, "filename": "orders.csv"},
        content=ORDERS_CSV,
    )
    assert anonymous.status_code == 401
    assert store(client).objects == {}


def test_only_raw_datasets_with_a_contract_can_land(client, demo):
    staging = land(client, demo, name="stg_orders")
    assert staging.status_code == 422
    assert staging.json()["error"]["message"] == "Only raw datasets have a landing zone"

    created = client.post(
        base(demo), json={"name": "raw_refunds", "layer": "raw"},
        headers=auth(demo["tokens"]["engineer"]),
    )
    assert created.status_code == 201
    no_contract = land(client, demo, name="raw_refunds")
    assert no_contract.status_code == 422
    assert no_contract.json()["error"]["message"] == (
        "Publish an ingestion contract before landing files"
    )


@pytest.mark.parametrize(
    "params",
    [
        {"filename": "orders.csv"},  # business date missing
        {"business_date": "2026-02-30", "filename": "orders.csv"},
        {"business_date": DATE, "filename": "../etc/passwd"},
        {"business_date": DATE, "filename": ""},
    ],
)
def test_query_parameters_are_validated(client, demo, params):
    response = client.post(
        files_url(client, demo), params=params, content=ORDERS_CSV,
        headers=auth(demo["tokens"]["engineer"]),
    )
    assert response.status_code == 422


def test_empty_and_oversized_files_are_refused(client, demo):
    empty = land(client, demo, data=b"")
    assert empty.status_code == 422
    assert empty.json()["error"]["message"] == "The landing file is empty"

    client.app.state.settings.max_landing_bytes = 64
    too_big = land(client, demo)
    assert too_big.status_code == 413
    assert too_big.json()["error"]["code"] == "payload_too_large"
    assert store(client).objects == {}


def test_storage_outage_answers_503_and_records_nothing(client, demo):
    store(client).fail = True

    response = land(client, demo)

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "unavailable"
    store(client).fail = False
    listed = client.get(files_url(client, demo), headers=auth(demo["tokens"]["viewer"]))
    assert listed.json()["items"] == []
    assert land(client, demo).status_code == 201  # a retry succeeds


def test_list_detail_and_business_date_filter(client, demo):
    first = land(client, demo).json()
    second_csv = ORDERS_CSV.replace(b"o-1,", b"o-3,").replace(b"o-2,", b"o-4,")
    second = land(client, demo, data=second_csv, business_date="2026-03-02").json()
    viewer = auth(demo["tokens"]["viewer"])

    items = client.get(files_url(client, demo), headers=viewer).json()["items"]
    assert [i["id"] for i in items] == [second["id"], first["id"]]
    assert "duplicate" not in items[0]

    filtered = client.get(
        files_url(client, demo), params={"business_date": DATE}, headers=viewer
    ).json()["items"]
    assert [i["id"] for i in filtered] == [first["id"]]

    detail = client.get(files_url(client, demo, suffix=f"/{first['id']}"), headers=viewer)
    assert detail.status_code == 200
    assert detail.json()["sha256"] == first["sha256"]

    other_dataset = client.get(
        files_url(client, demo, name="raw_payments", suffix=f"/{first['id']}"), headers=viewer
    )
    assert other_dataset.status_code == 404


def test_verify_detects_changed_or_missing_objects(client, demo):
    landed = land(client, demo).json()
    url = files_url(client, demo, suffix=f"/{landed['id']}/verify")
    engineer = auth(demo["tokens"]["engineer"])

    intact = client.post(url, headers=engineer).json()
    assert intact == {
        "intact": True, "expected_sha256": landed["sha256"], "actual_sha256": landed["sha256"]
    }

    objects = store(client).objects
    objects[landed["object_key"]] = (b"tampered", "text/csv")
    changed = client.post(url, headers=engineer).json()
    assert changed["intact"] is False
    assert changed["actual_sha256"] == hashlib.sha256(b"tampered").hexdigest()

    del objects[landed["object_key"]]
    missing = client.post(url, headers=engineer).json()
    assert missing == {
        "intact": False, "expected_sha256": landed["sha256"], "actual_sha256": None
    }

    assert client.post(url, headers=auth(demo["tokens"]["viewer"])).status_code == 403


def test_logs_never_contain_file_contents(client, demo, caplog):
    caplog.set_level(logging.INFO, logger="dataharbor_api.landing")
    land(client, demo)
    land(client, demo, data=ORDERS_CSV + ORDERS_CSV.splitlines(keepends=True)[1])

    assert "landed dataset=" in caplog.text
    assert "landing rejected dataset=" in caplog.text
    for value in (b"o-1", b"Harbor Goods", b"9.00"):
        assert value.decode() not in caplog.text


def test_memory_store_rejects_wrong_checksum():
    memory = MemoryObjectStore()
    with pytest.raises(StorageError):
        memory.put("k", b"data", content_type="text/plain", sha256="0" * 64)
    assert memory.head("k") is None


SOURCE_DATASETS = {"customers": "raw_customers", "orders": "raw_orders", "payments": "raw_payments"}


def test_generated_fixture_files_land_with_manifest_checksums(client, demo, fixtures_module):
    fixture_set = fixtures_module.generate(fixtures_module.Options(days=2))

    for entry in fixture_set.manifest["files"]:
        response = land(
            client, demo,
            data=fixture_set.files[entry["path"]],
            name=SOURCE_DATASETS[entry["source"]],
            business_date=entry["business_date"],
            filename=entry["path"].rsplit("/", 1)[-1],
            headers={"X-Content-SHA256": entry["sha256"]},
        )
        assert response.status_code == 201, (entry["path"], response.json())
        body = response.json()
        assert body["sha256"] == entry["sha256"]
        assert body["record_count"] == entry["record_count"]
        assert body["object_key"].endswith(f"{entry['sha256']}.{entry['format']}")

    assert len(store(client).objects) == len(fixture_set.manifest["files"])


def test_s3_store_sends_sha256_checksum_and_metadata():
    from dataharbor_api.storage import S3ObjectStore

    s3 = S3ObjectStore("http://127.0.0.1:9000", "key", "secret", "landing")
    calls = []
    s3._client.put_object = lambda *args, **kwargs: calls.append((args, kwargs))
    digest = hashlib.sha256(b"a,b\n").hexdigest()

    stored = s3.put("k.csv", b"a,b\n", content_type="text/csv", sha256=digest)

    (bucket, key, _), kwargs = calls[0]
    assert (bucket, key) == ("landing", "k.csv")
    assert kwargs["length"] == 4
    assert kwargs["metadata"]["sha256"] == digest
    assert kwargs["metadata"]["x-amz-checksum-sha256"] == (
        "W+CMloSh0l787gkxggSCQniwi7+0rvlz/+/QuddHgxM="
    )
    assert stored.sha256 == digest


def test_s3_store_maps_connection_errors_to_storage_error():
    from dataharbor_api.storage import S3ObjectStore

    s3 = S3ObjectStore("http://127.0.0.1:9", "key", "secret", "landing")
    s3._client._http.connection_pool_kw["retries"] = 0
    with pytest.raises(StorageError):
        s3.put("k", b"x", content_type="text/plain", sha256=hashlib.sha256(b"x").hexdigest())
    with pytest.raises(StorageError):
        s3.head("k")


def test_s3_store_rejects_bad_endpoint():
    from dataharbor_api.storage import S3ObjectStore

    with pytest.raises(ValueError):
        S3ObjectStore("127.0.0.1:9000", "key", "secret", "landing")
