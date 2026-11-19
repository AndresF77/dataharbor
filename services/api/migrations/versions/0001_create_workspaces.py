"""create workspaces

The tenant root: every tenant-owned table added later references
workspaces.id. Rollback drops the table, so it is only safe before any
dependent table exists (later migrations downgrade first).

Revision ID: 0001
Revises:
Create Date: 2026-11-19
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "workspaces",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("slug", sa.String(length=40), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
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
        sa.CheckConstraint("length(name) BETWEEN 1 AND 120", name="name_length"),
        sa.CheckConstraint("length(slug) BETWEEN 3 AND 40", name="slug_length"),
        sa.PrimaryKeyConstraint("id", name="pk_workspaces"),
        sa.UniqueConstraint("slug", name="uq_workspaces_slug"),
    )


def downgrade() -> None:
    op.drop_table("workspaces")
