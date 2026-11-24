"""create pipelines and run tracking

`pipelines` registers orchestrated jobs (the Airflow DAG id and schedule).
`pipeline_runs` gives every execution a stable run ID; the orchestrator's
own run id is unique per pipeline so a retried start is recognised, and a
run can only be finished once (`finished_at` is set exactly when the status
is terminal). `pipeline_run_tasks` holds one immutable outcome per task.
Rollback drops the three tables and with them the run history; landed
files and contracts are untouched.

Revision ID: 0006
Revises: 0005
Create Date: 2026-11-23
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "pipelines",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=63), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("dag_id", sa.String(length=250), nullable=False),
        sa.Column("schedule", sa.String(length=64), nullable=True),
        sa.Column("description", sa.String(length=2000), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint("length(name) BETWEEN 2 AND 63", name="name_length"),
        sa.CheckConstraint("kind IN ('ingestion', 'transformation')", name="kind_valid"),
        sa.CheckConstraint("length(dag_id) BETWEEN 1 AND 250", name="dag_id_length"),
        sa.CheckConstraint("length(description) <= 2000", name="description_length"),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name="fk_pipelines_workspace_id_workspaces",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_pipelines"),
        sa.UniqueConstraint("workspace_id", "name", name="uq_pipelines_workspace_id"),
        sa.UniqueConstraint("workspace_id", "dag_id", name="uq_pipelines_workspace_id_dag_id"),
    )
    op.create_table(
        "pipeline_runs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("pipeline_id", sa.Uuid(), nullable=False),
        sa.Column("external_run_id", sa.String(length=250), nullable=False),
        sa.Column("trigger", sa.String(length=16), nullable=False),
        sa.Column("business_date", sa.Date(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("started_by_user_id", sa.Uuid(), nullable=True),
        sa.Column("error_summary", sa.String(length=500), nullable=True),
        sa.Column(
            "started_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "length(external_run_id) BETWEEN 1 AND 250", name="external_run_id_length"
        ),
        sa.CheckConstraint(
            "trigger IN ('scheduled', 'manual', 'backfill')", name="trigger_valid"
        ),
        sa.CheckConstraint(
            "status IN ('running', 'succeeded', 'failed')", name="status_valid"
        ),
        sa.CheckConstraint(
            "(status = 'running') = (finished_at IS NULL)", name="finished_iff_terminal"
        ),
        sa.CheckConstraint(
            "error_summary IS NULL OR length(error_summary) <= 500", name="error_summary_length"
        ),
        sa.ForeignKeyConstraint(
            ["pipeline_id"],
            ["pipelines.id"],
            name="fk_pipeline_runs_pipeline_id_pipelines",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["started_by_user_id"],
            ["users.id"],
            name="fk_pipeline_runs_started_by_user_id_users",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_pipeline_runs"),
        sa.UniqueConstraint(
            "pipeline_id", "external_run_id", name="uq_pipeline_runs_pipeline_id"
        ),
    )
    op.create_table(
        "pipeline_run_tasks",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("task_id", sa.String(length=250), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("dataset_id", sa.Uuid(), nullable=True),
        sa.Column("landed_file_id", sa.Uuid(), nullable=True),
        sa.Column("message", sa.String(length=500), nullable=False),
        sa.Column(
            "recorded_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint("length(task_id) BETWEEN 1 AND 250", name="task_id_length"),
        sa.CheckConstraint(
            "status IN ('succeeded', 'failed', 'skipped')", name="status_valid"
        ),
        sa.CheckConstraint("length(message) <= 500", name="message_length"),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["pipeline_runs.id"],
            name="fk_pipeline_run_tasks_run_id_pipeline_runs",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["dataset_id"],
            ["datasets.id"],
            name="fk_pipeline_run_tasks_dataset_id_datasets",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["landed_file_id"],
            ["landed_files.id"],
            name="fk_pipeline_run_tasks_landed_file_id_landed_files",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_pipeline_run_tasks"),
        sa.UniqueConstraint("run_id", "task_id", name="uq_pipeline_run_tasks_run_id"),
    )


def downgrade() -> None:
    op.drop_table("pipeline_run_tasks")
    op.drop_table("pipeline_runs")
    op.drop_table("pipelines")
