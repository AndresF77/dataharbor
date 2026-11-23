"""ORM models. Every tenant-owned table references `workspaces.id`."""

import enum
import uuid
from datetime import date, datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    JSON,
    String,
    UniqueConstraint,
    Uuid,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from dataharbor_api.db import Base


class Workspace(Base):
    __tablename__ = "workspaces"
    __table_args__ = (
        CheckConstraint("length(name) BETWEEN 1 AND 120", name="name_length"),
        CheckConstraint("length(slug) BETWEEN 3 AND 40", name="slug_length"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    slug: Mapped[str] = mapped_column(String(40), unique=True)
    name: Mapped[str] = mapped_column(String(120))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class Role(str, enum.Enum):
    """Workspace roles, lowest to highest. Each role includes the ones below it."""

    VIEWER = "viewer"
    ENGINEER = "engineer"
    ADMIN = "admin"

    @property
    def rank(self) -> int:
        return list(Role).index(self)

    def includes(self, other: "Role") -> bool:
        return self.rank >= other.rank


ROLE_VALUES = ", ".join(f"'{role.value}'" for role in Role)


class User(Base):
    __tablename__ = "users"
    __table_args__ = (
        CheckConstraint("length(email) BETWEEN 3 AND 254", name="email_length"),
        CheckConstraint("email = lower(email)", name="email_lowercase"),
        CheckConstraint("length(display_name) BETWEEN 1 AND 120", name="display_name_length"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    email: Mapped[str] = mapped_column(String(254), unique=True)
    display_name: Mapped[str] = mapped_column(String(120))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class ApiToken(Base):
    """Bearer tokens. Only the SHA-256 of the token is stored."""

    __tablename__ = "api_tokens"
    __table_args__ = (
        CheckConstraint("length(token_hash) = 64", name="token_hash_length"),
        CheckConstraint("length(label) BETWEEN 1 AND 80", name="label_length"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    label: Mapped[str] = mapped_column(String(80))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class WorkspaceMember(Base):
    __tablename__ = "workspace_members"
    __table_args__ = (CheckConstraint(f"role IN ({ROLE_VALUES})", name="role_valid"),)

    workspace_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("workspaces.id", ondelete="CASCADE"), primary_key=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True, index=True
    )
    role: Mapped[str] = mapped_column(String(16))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class Layer(str, enum.Enum):
    """Where a dataset sits in the warehouse: landed, cleaned or business-facing."""

    RAW = "raw"
    STAGING = "staging"
    MART = "mart"


LAYER_VALUES = ", ".join(f"'{layer.value}'" for layer in Layer)


class Dataset(Base):
    """A dataset registered in a workspace. `version` guards concurrent edits."""

    __tablename__ = "datasets"
    __table_args__ = (
        CheckConstraint("length(name) BETWEEN 2 AND 63", name="name_length"),
        CheckConstraint(f"layer IN ({LAYER_VALUES})", name="layer_valid"),
        CheckConstraint("length(description) <= 2000", name="description_length"),
        CheckConstraint(
            "freshness_sla_minutes IS NULL OR freshness_sla_minutes BETWEEN 1 AND 525600",
            name="freshness_sla_range",
        ),
        CheckConstraint("version >= 1", name="version_positive"),
        UniqueConstraint("workspace_id", "name"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("workspaces.id", ondelete="CASCADE")
    )
    name: Mapped[str] = mapped_column(String(63))
    layer: Mapped[str] = mapped_column(String(16))
    description: Mapped[str] = mapped_column(String(2000), default="")
    owner_user_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="SET NULL"), index=True
    )
    freshness_sla_minutes: Mapped[int | None] = mapped_column(Integer)
    version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class IngestionContract(Base):
    """One published version of a raw dataset's source contract. Rows are never edited."""

    __tablename__ = "ingestion_contracts"
    __table_args__ = (
        CheckConstraint("version >= 1", name="version_positive"),
        CheckConstraint("format IN ('csv', 'jsonl')", name="format_valid"),
        UniqueConstraint("dataset_id", "version"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    dataset_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("datasets.id", ondelete="CASCADE")
    )
    version: Mapped[int] = mapped_column(Integer)
    format: Mapped[str] = mapped_column(String(8))
    columns: Mapped[list] = mapped_column(JSON)
    primary_key: Mapped[list] = mapped_column(JSON)
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class LandedFile(Base):
    """A source file stored in the landing bucket after passing its contract.

    The object key is derived from the SHA-256, and `(dataset_id, sha256)`
    is unique, so re-sending identical bytes never lands a second copy.
    Rows are never edited.
    """

    __tablename__ = "landed_files"
    __table_args__ = (
        CheckConstraint("length(sha256) = 64", name="sha256_length"),
        CheckConstraint("size_bytes > 0", name="size_positive"),
        CheckConstraint("record_count >= 0", name="record_count_nonnegative"),
        CheckConstraint("contract_version >= 1", name="contract_version_positive"),
        CheckConstraint("length(original_filename) BETWEEN 1 AND 255", name="filename_length"),
        UniqueConstraint("dataset_id", "sha256"),
        UniqueConstraint("object_key"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    dataset_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("datasets.id", ondelete="CASCADE")
    )
    contract_version: Mapped[int] = mapped_column(Integer)
    business_date: Mapped[date] = mapped_column(Date)
    original_filename: Mapped[str] = mapped_column(String(255))
    object_key: Mapped[str] = mapped_column(String(512))
    sha256: Mapped[str] = mapped_column(String(64))
    size_bytes: Mapped[int] = mapped_column(BigInteger)
    record_count: Mapped[int] = mapped_column(Integer)
    landed_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="SET NULL")
    )
    landed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class PipelineKind(str, enum.Enum):
    INGESTION = "ingestion"
    TRANSFORMATION = "transformation"


class RunTrigger(str, enum.Enum):
    SCHEDULED = "scheduled"
    MANUAL = "manual"
    BACKFILL = "backfill"


class RunStatus(str, enum.Enum):
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class TaskStatus(str, enum.Enum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    SKIPPED = "skipped"


def _values(enum_type: type[enum.Enum]) -> str:
    return ", ".join(f"'{member.value}'" for member in enum_type)


class Pipeline(Base):
    """An orchestrated job, e.g. the Airflow DAG that lands the raw sources."""

    __tablename__ = "pipelines"
    __table_args__ = (
        CheckConstraint("length(name) BETWEEN 2 AND 63", name="name_length"),
        CheckConstraint(f"kind IN ({_values(PipelineKind)})", name="kind_valid"),
        CheckConstraint("length(dag_id) BETWEEN 1 AND 250", name="dag_id_length"),
        CheckConstraint("length(description) <= 2000", name="description_length"),
        UniqueConstraint("workspace_id", "name"),
        UniqueConstraint("workspace_id", "dag_id", name="uq_pipelines_workspace_id_dag_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("workspaces.id", ondelete="CASCADE")
    )
    name: Mapped[str] = mapped_column(String(63))
    kind: Mapped[str] = mapped_column(String(16))
    dag_id: Mapped[str] = mapped_column(String(250))
    schedule: Mapped[str | None] = mapped_column(String(64))
    description: Mapped[str] = mapped_column(String(2000), default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class PipelineRun(Base):
    """One execution of a pipeline for a business date.

    `id` is DataHarbor's stable run ID; `external_run_id` is the orchestrator's
    (Airflow `dag_run_id`) and is unique per pipeline, so a retried "start"
    returns the same run. A run moves from `running` to `succeeded` or
    `failed` exactly once and is never edited afterwards.
    """

    __tablename__ = "pipeline_runs"
    __table_args__ = (
        CheckConstraint("length(external_run_id) BETWEEN 1 AND 250", name="external_run_id_length"),
        CheckConstraint(f"trigger IN ({_values(RunTrigger)})", name="trigger_valid"),
        CheckConstraint(f"status IN ({_values(RunStatus)})", name="status_valid"),
        CheckConstraint(
            "(status = 'running') = (finished_at IS NULL)", name="finished_iff_terminal"
        ),
        CheckConstraint(
            "error_summary IS NULL OR length(error_summary) <= 500", name="error_summary_length"
        ),
        UniqueConstraint("pipeline_id", "external_run_id"),
        Index("ix_pipeline_runs_history", "pipeline_id", "business_date", "id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    pipeline_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("pipelines.id", ondelete="CASCADE")
    )
    external_run_id: Mapped[str] = mapped_column(String(250))
    trigger: Mapped[str] = mapped_column(String(16))
    business_date: Mapped[date] = mapped_column(Date)
    status: Mapped[str] = mapped_column(String(16), default=RunStatus.RUNNING.value)
    started_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="SET NULL")
    )
    error_summary: Mapped[str | None] = mapped_column(String(500))
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class PipelineRunTask(Base):
    """The recorded outcome of one task in a run. Written once, never edited."""

    __tablename__ = "pipeline_run_tasks"
    __table_args__ = (
        CheckConstraint("length(task_id) BETWEEN 1 AND 250", name="task_id_length"),
        CheckConstraint(f"status IN ({_values(TaskStatus)})", name="status_valid"),
        CheckConstraint("length(message) <= 500", name="message_length"),
        UniqueConstraint("run_id", "task_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    run_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("pipeline_runs.id", ondelete="CASCADE")
    )
    task_id: Mapped[str] = mapped_column(String(250))
    status: Mapped[str] = mapped_column(String(16))
    dataset_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("datasets.id", ondelete="SET NULL")
    )
    landed_file_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("landed_files.id", ondelete="SET NULL")
    )
    message: Mapped[str] = mapped_column(String(500), default="")
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
