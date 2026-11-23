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
    } <= tables(database_url)


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
