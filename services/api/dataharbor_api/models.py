"""ORM models. Every tenant-owned table references `workspaces.id`."""

import enum
import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
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
