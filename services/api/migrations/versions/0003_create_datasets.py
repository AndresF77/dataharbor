"""create datasets

The dataset registry: one row per dataset a workspace tracks, with its
layer (`raw`, `staging`, `mart`), optional owner and freshness target.
`version` is bumped on every update for optimistic concurrency. Dataset
names are unique per workspace. Rollback drops the table and every
registered dataset; workspaces, users and members are kept.

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-19
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "datasets",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=63), nullable=False),
        sa.Column("layer", sa.String(length=16), nullable=False),
        sa.Column("description", sa.String(length=2000), nullable=False),
        sa.Column("owner_user_id", sa.Uuid(), nullable=True),
        sa.Column("freshness_sla_minutes", sa.Integer(), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint("length(name) BETWEEN 2 AND 63", name="name_length"),
        sa.CheckConstraint("layer IN ('raw', 'staging', 'mart')", name="layer_valid"),
        sa.CheckConstraint("length(description) <= 2000", name="description_length"),
        sa.CheckConstraint(
            "freshness_sla_minutes IS NULL OR freshness_sla_minutes BETWEEN 1 AND 525600",
            name="freshness_sla_range",
        ),
        sa.CheckConstraint("version >= 1", name="version_positive"),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name="fk_datasets_workspace_id_workspaces",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["owner_user_id"],
            ["users.id"],
            name="fk_datasets_owner_user_id_users",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_datasets"),
        sa.UniqueConstraint("workspace_id", "name", name="uq_datasets_workspace_id"),
    )
    op.create_index("ix_datasets_owner_user_id", "datasets", ["owner_user_id"])


def downgrade() -> None:
    op.drop_index("ix_datasets_owner_user_id", table_name="datasets")
    op.drop_table("datasets")
