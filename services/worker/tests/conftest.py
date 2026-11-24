import os
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from dataharbor_api.config import Settings
from dataharbor_api.main import create_app
from dataharbor_api.migrations import upgrade
from dataharbor_api.seed import seed_demo
from dataharbor_worker.api_client import ApiClient
from dataharbor_worker.warehouse import connection_settings

REPO_ROOT = Path(__file__).resolve().parents[3]

# Schemas the loader and dbt write; dropped before and after each warehouse test.
WAREHOUSE_SCHEMAS = ("raw", "staging", "mart")


class TestClientTransport:
    """Sends worker requests to the in-process FastAPI app."""

    def __init__(self, client: TestClient):
        self.client = client

    def __call__(self, method, path, *, params=None, json_body=None, content=None, headers=None):
        response = self.client.request(
            method, path, params=params, json=json_body, content=content, headers=headers
        )
        return response.status_code, response.json() if response.content else None


@pytest.fixture
def api(tmp_path: Path) -> Iterator[TestClient]:
    database_url = f"sqlite+pysqlite:///{tmp_path / 'dataharbor.db'}"
    app = create_app(Settings(database_url=database_url, environment="test", object_store="memory"))
    upgrade(database_url)
    with TestClient(app) as client:
        with app.state.database.new_session() as session:
            client.tokens = dict(zip(("admin", "engineer", "viewer"),
                                     seed_demo(session).values(), strict=True))
        headers = {"Authorization": f"Bearer {client.tokens['viewer']}"}
        client.workspace_id = client.get("/v1/workspaces", headers=headers).json()[0]["id"]
        yield client


@pytest.fixture
def worker_client(api) -> ApiClient:
    return ApiClient(TestClientTransport(api), api.tokens["engineer"], api.workspace_id)


@pytest.fixture(scope="session")
def fixtures_module():
    """`packages/fixtures` generator, imported from the repository checkout."""
    path = str(REPO_ROOT / "packages" / "fixtures")
    sys.path.insert(0, path)
    try:
        yield pytest.importorskip("dataharbor_fixtures.generator")
    finally:
        sys.path.remove(path)


@pytest.fixture
def make_sources(tmp_path, fixtures_module):
    """Write a fixture set to a fresh directory; returns (directory, options)."""

    def make(defects: bool = False, name: str = "sources"):
        options = fixtures_module.Options(defects=defects)
        out = tmp_path / name
        fixtures_module.write(fixtures_module.generate(options), out)
        return out, options

    return make


@pytest.fixture
def warehouse_env() -> dict[str, str]:
    """DATAHARBOR_WAREHOUSE_* settings taken from DATAHARBOR_TEST_WAREHOUSE_*."""
    env = {k.replace("_TEST_", "_"): v for k, v in os.environ.items()
           if k.startswith("DATAHARBOR_TEST_WAREHOUSE_")}
    if not env:
        pytest.skip("set DATAHARBOR_TEST_WAREHOUSE_* to run against PostgreSQL")
    return env


def _drop_schemas(conn) -> None:
    for schema in WAREHOUSE_SCHEMAS:
        conn.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
    conn.commit()


@pytest.fixture
def warehouse(warehouse_env):
    """A real PostgreSQL warehouse (scratch database: raw/staging/mart are dropped)."""
    psycopg = pytest.importorskip("psycopg")
    conn = psycopg.connect(**connection_settings(warehouse_env))
    _drop_schemas(conn)
    yield conn
    conn.rollback()
    _drop_schemas(conn)
    conn.close()
