"""create ingestion contracts

Versioned source contracts for raw datasets: file format, typed columns
and primary key. Each publish inserts a new `version`; earlier versions
stay readable so a landed file can always be checked against the
contract it arrived under. Rollback drops the table and every contract
version; datasets are kept.

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-19
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "ingestion_contracts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("dataset_id", sa.Uuid(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("format", sa.String(length=8), nullable=False),
        sa.Column("columns", sa.JSON(), nullable=False),
        sa.Column("primary_key", sa.JSON(), nullable=False),
        sa.Column("created_by_user_id", sa.Uuid(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint("version >= 1", name="version_positive"),
        sa.CheckConstraint("format IN ('csv', 'jsonl')", name="format_valid"),
        sa.ForeignKeyConstraint(
            ["dataset_id"],
            ["datasets.id"],
            name="fk_ingestion_contracts_dataset_id_datasets",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_user_id"],
            ["users.id"],
            name="fk_ingestion_contracts_created_by_user_id_users",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_ingestion_contracts"),
        sa.UniqueConstraint(
            "dataset_id", "version", name="uq_ingestion_contracts_dataset_id"
        ),
    )


def downgrade() -> None:
    op.drop_table("ingestion_contracts")
