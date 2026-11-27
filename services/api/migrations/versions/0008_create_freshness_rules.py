"""create freshness rules

`freshness_rules` holds at most one rule per dataset: which signal marks the
dataset as updated (a landed file, or a successful run of a given pipeline)
and after how many minutes without an update it is `warning` / `stale`.
A rule that watches a pipeline needs the pipeline (and only then); the warn
threshold must be below the error threshold. Rules go with their dataset;
a pipeline that a rule watches cannot be deleted. Rollback drops the table
and with it the rules only; datasets keep their `freshness_sla_minutes`.

Revision ID: 0008
Revises: 0007
Create Date: 2026-11-27
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "freshness_rules",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("dataset_id", sa.Uuid(), nullable=False),
        sa.Column("signal", sa.String(length=16), nullable=False),
        sa.Column("pipeline_id", sa.Uuid(), nullable=True),
        sa.Column("warn_after_minutes", sa.Integer(), nullable=True),
        sa.Column("error_after_minutes", sa.Integer(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("updated_by_user_id", sa.Uuid(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint("signal IN ('landed_file', 'pipeline_run')", name="signal_valid"),
        sa.CheckConstraint(
            "(signal = 'pipeline_run') = (pipeline_id IS NOT NULL)", name="pipeline_iff_run_signal"
        ),
        sa.CheckConstraint("error_after_minutes BETWEEN 1 AND 525600", name="error_after_range"),
        sa.CheckConstraint(
            "warn_after_minutes IS NULL OR "
            "(warn_after_minutes >= 1 AND warn_after_minutes < error_after_minutes)",
            name="warn_before_error",
        ),
        sa.CheckConstraint("version >= 1", name="version_positive"),
        sa.ForeignKeyConstraint(
            ["dataset_id"],
            ["datasets.id"],
            name="fk_freshness_rules_dataset_id_datasets",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["pipeline_id"],
            ["pipelines.id"],
            name="fk_freshness_rules_pipeline_id_pipelines",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["updated_by_user_id"],
            ["users.id"],
            name="fk_freshness_rules_updated_by_user_id_users",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_freshness_rules"),
        sa.UniqueConstraint("dataset_id", name="uq_freshness_rules_dataset_id"),
    )
    op.create_index(
        "ix_freshness_rules_pipeline_id", "freshness_rules", ["pipeline_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_freshness_rules_pipeline_id", table_name="freshness_rules")
    op.drop_table("freshness_rules")
