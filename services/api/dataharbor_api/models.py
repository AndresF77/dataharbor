"""ORM models. Every tenant-owned table references `workspaces.id`."""

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, String, Uuid, func
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
