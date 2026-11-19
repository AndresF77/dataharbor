import sys
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from dataharbor_api.config import Settings
from dataharbor_api.main import create_app
from dataharbor_api.migrations import upgrade
from dataharbor_api.seed import seed_demo
from tests.helpers import auth

REPO_ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture
def database_url(tmp_path: Path) -> str:
    # SQLite keeps unit tests self-contained; the same migrations target PostgreSQL.
    return f"sqlite+pysqlite:///{tmp_path / 'dataharbor.db'}"


@pytest.fixture
def client(database_url: str) -> Iterator[TestClient]:
    app = create_app(
        Settings(database_url=database_url, environment="test", object_store="memory")
    )
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def demo(client, database_url):
    """Migrated database with the seeded demo workspace; returns tokens by role."""
    upgrade(database_url)
    with client.app.state.database.new_session() as session:
        tokens = seed_demo(session)
    by_role = dict(zip(("admin", "engineer", "viewer"), tokens.values(), strict=True))
    workspace = client.get("/v1/workspaces", headers=auth(by_role["viewer"])).json()[0]
    return {"tokens": by_role, "workspace_id": workspace["id"]}


@pytest.fixture
def fixtures_module():
    """`packages/fixtures` generator, imported from the repository checkout."""
    path = str(REPO_ROOT / "packages" / "fixtures")
    sys.path.insert(0, path)
    try:
        yield pytest.importorskip("dataharbor_fixtures.generator")
    finally:
        sys.path.remove(path)
