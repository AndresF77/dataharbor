import uuid

from fastapi.testclient import TestClient

from dataharbor_api.config import Settings
from dataharbor_api.main import create_app
from dataharbor_api.migrations import upgrade


def test_liveness_does_not_need_a_database(client):
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_readiness_reports_pending_migrations(client):
    response = client.get("/health/ready")

    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "not_ready"
    assert body["checks"] == {"database": "ok", "migrations": "pending"}
    assert body["schema_revision"] is None
    assert body["expected_revision"] == "0004"


def test_readiness_is_ready_after_migrating(client, database_url):
    upgrade(database_url)

    response = client.get("/health/ready")

    assert response.status_code == 200
    assert response.json()["checks"] == {"database": "ok", "migrations": "ok"}
    assert response.json()["schema_revision"] == "0004"


def test_readiness_reports_an_unreachable_database(tmp_path):
    missing = tmp_path / "no-such-dir" / "db.sqlite"
    app = create_app(Settings(database_url=f"sqlite+pysqlite:///{missing}"))
    with TestClient(app) as client:
        response = client.get("/health/ready")

    assert response.status_code == 503
    assert response.json()["checks"] == {"database": "unreachable"}


def test_safe_inbound_request_id_is_echoed(client):
    response = client.get("/health", headers={"X-Request-ID": "demo-req-0001"})

    assert response.headers["x-request-id"] == "demo-req-0001"


def test_unsafe_request_id_is_replaced(client):
    response = client.get("/health", headers={"X-Request-ID": "bad id\twith spaces"})

    generated = response.headers["x-request-id"]
    assert generated != "bad id\twith spaces"
    assert uuid.UUID(hex=generated)


def test_unknown_route_uses_the_error_envelope(client):
    response = client.get("/v1/nope", headers={"X-Request-ID": "demo-req-0002"})

    assert response.status_code == 404
    assert response.json() == {
        "error": {
            "code": "not_found",
            "message": "Not Found",
            "request_id": "demo-req-0002",
            "details": None,
        }
    }


def test_openapi_contract_is_versioned(client):
    response = client.get("/v1/openapi.json")

    assert response.status_code == 200
    assert {"/health", "/health/ready"} <= set(response.json()["paths"])
