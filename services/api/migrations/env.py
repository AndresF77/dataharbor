"""Alembic environment. The URL comes from the caller (tests, code) or
DATAHARBOR_DATABASE_URL; it is never stored in alembic.ini."""

from alembic import context
from sqlalchemy import create_engine

from dataharbor_api import models  # noqa: F401  (registers tables on Base.metadata)
from dataharbor_api.config import get_settings
from dataharbor_api.db import Base

config = context.config
target_metadata = Base.metadata


def database_url() -> str:
    return config.attributes.get("database_url") or get_settings().database_url


def run_offline() -> None:
    context.configure(
        url=database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_online() -> None:
    engine = create_engine(database_url())
    with engine.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=connection.dialect.name == "sqlite",
        )
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()


if context.is_offline_mode():
    run_offline()
else:
    run_online()
