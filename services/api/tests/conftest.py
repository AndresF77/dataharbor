from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from dataharbor_api.config import Settings
from dataharbor_api.main import create_app


@pytest.fixture
def database_url(tmp_path: Path) -> str:
    # SQLite keeps unit tests self-contained; the same migrations target PostgreSQL.
    return f"sqlite+pysqlite:///{tmp_path / 'dataharbor.db'}"


@pytest.fixture
def client(database_url: str) -> Iterator[TestClient]:
    app = create_app(Settings(database_url=database_url, environment="test"))
    with TestClient(app) as test_client:
        yield test_client
