import uuid

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError

from dataharbor_api.migrations import downgrade, upgrade


def tables(database_url: str) -> set[str]:
    engine = create_engine(database_url)
    try:
        return set(inspect(engine).get_table_names())
    finally:
        engine.dispose()


def test_upgrade_creates_workspaces(database_url):
    upgrade(database_url)

    assert {
        "alembic_version",
        "workspaces",
        "users",
        "api_tokens",
        "workspace_members",
        "datasets",
        "ingestion_contracts",
        "landed_files",
        "pipelines",
        "pipeline_runs",
        "pipeline_run_tasks",
        "freshness_rules",
        "quality_rules",
        "quality_results",
    } <= tables(database_url)


def test_downgrade_0009_drops_quality_tables_only(database_url):
    upgrade(database_url)
    downgrade(database_url, "0008")

    remaining = tables(database_url)
    assert not {"quality_rules", "quality_results"} & remaining
    assert {"freshness_rules", "landed_files", "datasets"} <= remaining


def test_downgrade_0008_drops_freshness_rules_only(database_url):
    upgrade(database_url)
    downgrade(database_url, "0007")

    remaining = tables(database_url)
    assert "freshness_rules" not in remaining
    assert {"datasets", "pipelines", "pipeline_runs"} <= remaining


def test_downgrade_0006_keeps_landed_files(database_url):
    upgrade(database_url)
    downgrade(database_url, "0005")

    remaining = tables(database_url)
    assert "landed_files" in remaining
    assert not {"pipelines", "pipeline_runs", "pipeline_run_tasks"} & remaining


def test_downgrade_0007_drops_history_index_only(database_url):
    upgrade(database_url)

    def indexes() -> set[str]:
        engine = create_engine(database_url)
        try:
            return {i["name"] for i in inspect(engine).get_indexes("pipeline_runs")}
        finally:
            engine.dispose()

    assert "ix_pipeline_runs_history" in indexes()
    downgrade(database_url, "0006")
    assert "ix_pipeline_runs_history" not in indexes()
    assert "pipeline_runs" in tables(database_url)


def test_downgrade_0005_keeps_contracts(database_url):
    upgrade(database_url)
    downgrade(database_url, "0004")

    remaining = tables(database_url)
    assert "ingestion_contracts" in remaining
    assert "landed_files" not in remaining


def test_downgrade_0004_keeps_datasets(database_url):
    upgrade(database_url)
    downgrade(database_url, "0003")

    remaining = tables(database_url)
    assert "datasets" in remaining
    assert "ingestion_contracts" not in remaining


def test_downgrade_0003_keeps_members(database_url):
    upgrade(database_url)
    downgrade(database_url, "0002")

    remaining = tables(database_url)
    assert {"workspaces", "users", "workspace_members"} <= remaining
    assert "datasets" not in remaining


def test_downgrade_0002_keeps_workspaces(database_url):
    upgrade(database_url)
    downgrade(database_url, "0001")

    remaining = tables(database_url)
    assert "workspaces" in remaining
    assert not {"users", "api_tokens", "workspace_members"} & remaining


def test_downgrade_to_base_and_upgrade_again(database_url):
    upgrade(database_url)
    downgrade(database_url, "base")
    assert tables(database_url) <= {"alembic_version"}

    upgrade(database_url)
    assert {"workspaces", "workspace_members"} <= tables(database_url)


def insert_workspace(connection, slug: str, name: str) -> None:
    connection.execute(
        text("INSERT INTO workspaces (id, slug, name) VALUES (:id, :slug, :name)"),
        {"id": uuid.uuid4().hex, "slug": slug, "name": name},
    )


def test_workspace_slug_is_unique(database_url):
    upgrade(database_url)
    engine = create_engine(database_url)
    try:
        with engine.begin() as connection:
            insert_workspace(connection, "acme-analytics", "Acme Analytics")
        with pytest.raises(IntegrityError), engine.begin() as connection:
            insert_workspace(connection, "acme-analytics", "Another Acme")
    finally:
        engine.dispose()


@pytest.mark.parametrize(("slug", "name"), [("ab", "Too short slug"), ("acme", "")])
def test_workspace_check_constraints(database_url, slug, name):
    upgrade(database_url)
    engine = create_engine(database_url)
    try:
        with pytest.raises(IntegrityError), engine.begin() as connection:
            insert_workspace(connection, slug, name)
    finally:
        engine.dispose()


def test_model_metadata_matches_migrations(database_url):
    from alembic.autogenerate import compare_metadata
    from alembic.runtime.migration import MigrationContext

    from dataharbor_api.db import Base

    upgrade(database_url)
    engine = create_engine(database_url)
    try:
        with engine.connect() as connection:
            diff = compare_metadata(MigrationContext.configure(connection), Base.metadata)
    finally:
        engine.dispose()

    assert diff == []


def test_member_role_must_be_known(database_url):
    upgrade(database_url)
    engine = create_engine(database_url)
    workspace_id, user_id = uuid.uuid4().hex, uuid.uuid4().hex
    try:
        with engine.begin() as connection:
            insert_workspace(connection, "acme-analytics", "Acme Analytics")
            connection.execute(
                text("UPDATE workspaces SET id = :id"), {"id": workspace_id}
            )
            connection.execute(
                text("INSERT INTO users (id, email, display_name) VALUES (:id, :e, :n)"),
                {"id": user_id, "e": "erin@example.test", "n": "Erin"},
            )
        with pytest.raises(IntegrityError), engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO workspace_members (workspace_id, user_id, role) "
                    "VALUES (:w, :u, 'owner')"
                ),
                {"w": workspace_id, "u": user_id},
            )
    finally:
        engine.dispose()


def test_user_email_must_be_lowercase(database_url):
    upgrade(database_url)
    engine = create_engine(database_url)
    try:
        with pytest.raises(IntegrityError), engine.begin() as connection:
            connection.execute(
                text("INSERT INTO users (id, email, display_name) VALUES (:id, :e, :n)"),
                {"id": uuid.uuid4().hex, "e": "Erin@Example.test", "n": "Erin"},
            )
    finally:
        engine.dispose()


def _dataset_and_pipeline(connection) -> tuple[str, str]:
    workspace_id, dataset_id, pipeline_id = (uuid.uuid4().hex for _ in range(3))
    connection.execute(
        text("INSERT INTO workspaces (id, slug, name) VALUES (:id, 'acme', 'Acme')"),
        {"id": workspace_id},
    )
    connection.execute(
        text(
            "INSERT INTO datasets (id, workspace_id, name, layer, description, version) "
            "VALUES (:id, :w, 'stg_orders', 'staging', '', 1)"
        ),
        {"id": dataset_id, "w": workspace_id},
    )
    connection.execute(
        text(
            "INSERT INTO pipelines (id, workspace_id, name, kind, dag_id, description) "
            "VALUES (:id, :w, 'transform', 'transformation', 'transform', '')"
        ),
        {"id": pipeline_id, "w": workspace_id},
    )
    return dataset_id, pipeline_id


INSERT_RULE = text(
    "INSERT INTO freshness_rules "
    "(id, dataset_id, signal, pipeline_id, warn_after_minutes, error_after_minutes, version) "
    "VALUES (:id, :d, :signal, :p, :warn, :error, 1)"
)


@pytest.mark.parametrize(
    ("signal", "with_pipeline", "warn", "error"),
    [
        ("pipeline_run", False, None, 60),  # run signal without a pipeline
        ("landed_file", True, None, 60),  # pipeline on a file signal
        ("hourly", False, None, 60),
        ("landed_file", False, 60, 60),  # warn not below error
        ("landed_file", False, 0, 60),
        ("landed_file", False, None, 0),
        ("landed_file", False, None, 525601),
    ],
)
def test_freshness_rule_check_constraints(database_url, signal, with_pipeline, warn, error):
    upgrade(database_url)
    engine = create_engine(database_url)
    try:
        with engine.begin() as connection:
            dataset_id, pipeline_id = _dataset_and_pipeline(connection)
        with pytest.raises(IntegrityError), engine.begin() as connection:
            connection.execute(
                INSERT_RULE,
                {"id": uuid.uuid4().hex, "d": dataset_id, "signal": signal,
                 "p": pipeline_id if with_pipeline else None, "warn": warn, "error": error},
            )
    finally:
        engine.dispose()


def test_one_freshness_rule_per_dataset(database_url):
    upgrade(database_url)
    engine = create_engine(database_url)
    try:
        with engine.begin() as connection:
            dataset_id, pipeline_id = _dataset_and_pipeline(connection)
            connection.execute(
                INSERT_RULE,
                {"id": uuid.uuid4().hex, "d": dataset_id, "signal": "pipeline_run",
                 "p": pipeline_id, "warn": 30, "error": 60},
            )
        with pytest.raises(IntegrityError), engine.begin() as connection:
            connection.execute(
                INSERT_RULE,
                {"id": uuid.uuid4().hex, "d": dataset_id, "signal": "landed_file",
                 "p": None, "warn": None, "error": 60},
            )
    finally:
        engine.dispose()


def _landed_file_and_rule(connection) -> tuple[str, str, str]:
    dataset_id, _ = _dataset_and_pipeline(connection)
    file_id, rule_id = uuid.uuid4().hex, uuid.uuid4().hex
    connection.execute(
        text(
            "INSERT INTO landed_files (id, dataset_id, contract_version, business_date, "
            "original_filename, object_key, sha256, size_bytes, record_count) "
            "VALUES (:id, :d, 1, '2026-03-01', 'o.csv', 'k', :sha, 10, 2)"
        ),
        {"id": file_id, "d": dataset_id, "sha": "0" * 64},
    )
    connection.execute(
        text(
            "INSERT INTO quality_rules (id, dataset_id, name, check_kind, columns, params, "
            "severity, description, enabled, version) "
            "VALUES (:id, :d, 'has_rows', 'row_count', '[]', '{\"min\": \"1\"}', 'error', '', "
            ":enabled, 1)"
        ),
        {"id": rule_id, "d": dataset_id, "enabled": True},
    )
    return dataset_id, file_id, rule_id


INSERT_RESULT = text(
    "INSERT INTO quality_results (id, dataset_id, landed_file_id, rule_id, rule_name, "
    "rule_version, check_kind, severity, status, checked_records, failing_records, "
    "sample_records, message) "
    "VALUES (:id, :d, :f, :r, 'has_rows', :version, :check, 'error', :status, :checked, "
    ":failing, '[]', '')"
)


@pytest.mark.parametrize(
    ("status", "check", "version", "checked", "failing"),
    [
        ("pass", "row_count", 1, 3, 1),  # a pass with failures
        ("fail", "row_count", 1, 1, 2),  # more failures than checked records
        ("fail", "row_count", 1, 0, -1),
        ("skipped", "row_count", 1, 0, 0),
        ("fail", "regex", 1, 0, 0),
        ("fail", "row_count", 0, 0, 0),
    ],
)
def test_quality_result_check_constraints(database_url, status, check, version, checked,
                                          failing):
    upgrade(database_url)
    engine = create_engine(database_url)
    try:
        with engine.begin() as connection:
            dataset_id, file_id, rule_id = _landed_file_and_rule(connection)
        with pytest.raises(IntegrityError), engine.begin() as connection:
            connection.execute(INSERT_RESULT, {
                "id": uuid.uuid4().hex, "d": dataset_id, "f": file_id, "r": rule_id,
                "version": version, "check": check, "status": status,
                "checked": checked, "failing": failing,
            })
    finally:
        engine.dispose()


def test_one_result_per_file_and_rule_version(database_url):
    upgrade(database_url)
    engine = create_engine(database_url)
    try:
        with engine.begin() as connection:
            dataset_id, file_id, rule_id = _landed_file_and_rule(connection)
        values = {"d": dataset_id, "f": file_id, "r": rule_id, "version": 1,
                  "check": "row_count", "status": "pass", "checked": 2, "failing": 0}
        with engine.begin() as connection:
            connection.execute(INSERT_RESULT, {**values, "id": uuid.uuid4().hex})
            connection.execute(INSERT_RESULT, {**values, "id": uuid.uuid4().hex, "version": 2})
        with pytest.raises(IntegrityError), engine.begin() as connection:
            connection.execute(INSERT_RESULT, {**values, "id": uuid.uuid4().hex})
    finally:
        engine.dispose()
