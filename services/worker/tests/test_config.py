from pathlib import Path

import pytest

from dataharbor_worker.config import ConfigError, WorkerConfig

ENV = {
    "DATAHARBOR_API_URL": "http://127.0.0.1:8000",
    "DATAHARBOR_API_TOKEN": "dh_secret_token",
    "DATAHARBOR_WORKSPACE_ID": "6f1c2c1e-0000-4000-8000-000000000000",
    "DATAHARBOR_SOURCE_DIR": "/opt/airflow/sources",
}


def test_reads_environment():
    config = WorkerConfig.from_env(ENV)

    assert config.source_dir == Path("/opt/airflow/sources")
    assert "dh_secret_token" not in repr(config)


def test_lists_missing_variables():
    env = {**ENV, "DATAHARBOR_API_TOKEN": " ", "DATAHARBOR_SOURCE_DIR": ""}

    with pytest.raises(ConfigError, match="DATAHARBOR_API_TOKEN, DATAHARBOR_SOURCE_DIR"):
        WorkerConfig.from_env(env)


def test_rejects_bad_timeout():
    with pytest.raises(ConfigError):
        WorkerConfig.from_env({**ENV, "DATAHARBOR_API_TIMEOUT_SECONDS": "soon"})
