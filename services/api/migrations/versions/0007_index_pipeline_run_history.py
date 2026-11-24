"""index pipeline run history

Run history is read newest business date first, per pipeline, with keyset
pagination on `(business_date, id)`. `ix_pipeline_runs_history` covers that
order (PostgreSQL scans it backwards). Rollback drops the index only; no
data changes.

Revision ID: 0007
Revises: 0006
Create Date: 2026-11-23
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(
        "ix_pipeline_runs_history",
        "pipeline_runs",
        ["pipeline_id", "business_date", "id"],
    )


def downgrade() -> None:
    op.drop_index("ix_pipeline_runs_history", table_name="pipeline_runs")
