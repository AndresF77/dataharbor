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

    assert {"alembic_version", "workspaces"} <= tables(database_url)


def test_downgrade_to_base_and_upgrade_again(database_url):
    upgrade(database_url)
    downgrade(database_url, "base")
    assert "workspaces" not in tables(database_url)

    upgrade(database_url)
    assert "workspaces" in tables(database_url)


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
