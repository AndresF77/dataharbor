"""create quality rules and results

`quality_rules` holds named data quality checks on a raw dataset's landed
files (`not_null`, `unique`, `accepted_values`, `value_range`, `row_count`),
unique by name per dataset, with a severity (`warn`/`error`) and a version
that goes up on every change.

`quality_results` records one rule version evaluated on one landed file
(`pass`/`fail`/`error`, checked and failing record counts, up to 20 failing
record numbers; never values). `(landed_file_id, rule_id, rule_version)` is
unique, so re-running checks never duplicates a result. Results keep the
rule's name, check and severity, and survive a deleted rule (`rule_id` is
set to null). Rollback drops both tables and their history only.

Revision ID: 0009
Revises: 0008
Create Date: 2026-11-27
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CHECKS = "'not_null', 'unique', 'accepted_values', 'value_range', 'row_count'"
SEVERITIES = "'warn', 'error'"


def upgrade() -> None:
    op.create_table(
        "quality_rules",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("dataset_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=63), nullable=False),
        sa.Column("check_kind", sa.String(length=16), nullable=False),
        sa.Column("columns", sa.JSON(), nullable=False),
        sa.Column("params", sa.JSON(), nullable=False),
        sa.Column("severity", sa.String(length=8), nullable=False),
        sa.Column("description", sa.String(length=500), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("updated_by_user_id", sa.Uuid(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint("length(name) BETWEEN 2 AND 63", name="name_length"),
        sa.CheckConstraint(f"check_kind IN ({CHECKS})", name="check_kind_valid"),
        sa.CheckConstraint(f"severity IN ({SEVERITIES})", name="severity_valid"),
        sa.CheckConstraint("length(description) <= 500", name="description_length"),
        sa.CheckConstraint("version >= 1", name="version_positive"),
        sa.ForeignKeyConstraint(
            ["dataset_id"],
            ["datasets.id"],
            name="fk_quality_rules_dataset_id_datasets",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["updated_by_user_id"],
            ["users.id"],
            name="fk_quality_rules_updated_by_user_id_users",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_quality_rules"),
        sa.UniqueConstraint("dataset_id", "name", name="uq_quality_rules_dataset_id"),
    )
    op.create_table(
        "quality_results",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("dataset_id", sa.Uuid(), nullable=False),
        sa.Column("landed_file_id", sa.Uuid(), nullable=False),
        sa.Column("rule_id", sa.Uuid(), nullable=True),
        sa.Column("rule_name", sa.String(length=63), nullable=False),
        sa.Column("rule_version", sa.Integer(), nullable=False),
        sa.Column("check_kind", sa.String(length=16), nullable=False),
        sa.Column("severity", sa.String(length=8), nullable=False),
        sa.Column("status", sa.String(length=8), nullable=False),
        sa.Column("checked_records", sa.Integer(), nullable=False),
        sa.Column("failing_records", sa.Integer(), nullable=False),
        sa.Column("sample_records", sa.JSON(), nullable=False),
        sa.Column("message", sa.String(length=500), nullable=False),
        sa.Column("evaluated_by_user_id", sa.Uuid(), nullable=True),
        sa.Column(
            "evaluated_at", sa.DateTime(timezone=True), server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint("status IN ('pass', 'fail', 'error')", name="status_valid"),
        sa.CheckConstraint(f"check_kind IN ({CHECKS})", name="check_kind_valid"),
        sa.CheckConstraint(f"severity IN ({SEVERITIES})", name="severity_valid"),
        sa.CheckConstraint("rule_version >= 1", name="rule_version_positive"),
        sa.CheckConstraint(
            "checked_records >= 0 AND failing_records >= 0 "
            "AND failing_records <= checked_records",
            name="record_counts_valid",
        ),
        sa.CheckConstraint("status <> 'pass' OR failing_records = 0", name="pass_has_no_failures"),
        sa.CheckConstraint("length(message) <= 500", name="message_length"),
        sa.ForeignKeyConstraint(
            ["dataset_id"],
            ["datasets.id"],
            name="fk_quality_results_dataset_id_datasets",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["landed_file_id"],
            ["landed_files.id"],
            name="fk_quality_results_landed_file_id_landed_files",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["rule_id"],
            ["quality_rules.id"],
            name="fk_quality_results_rule_id_quality_rules",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["evaluated_by_user_id"],
            ["users.id"],
            name="fk_quality_results_evaluated_by_user_id_users",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_quality_results"),
        sa.UniqueConstraint(
            "landed_file_id", "rule_id", "rule_version", name="uq_quality_results_landed_file_id"
        ),
    )
    op.create_index("ix_quality_results_rule_id", "quality_results", ["rule_id"])
    op.create_index(
        "ix_quality_results_dataset_history", "quality_results", ["dataset_id", "evaluated_at"]
    )


def downgrade() -> None:
    op.drop_index("ix_quality_results_dataset_history", table_name="quality_results")
    op.drop_index("ix_quality_results_rule_id", table_name="quality_results")
    op.drop_table("quality_results")
    op.drop_table("quality_rules")
