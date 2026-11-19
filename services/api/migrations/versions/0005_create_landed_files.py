"""create landed files

Registry of source files stored in the MinIO landing bucket: the dataset,
the contract version the file passed, business date, object key, SHA-256,
size and record count. `(dataset_id, sha256)` is unique so identical
re-uploads are recognised. Rollback drops the table only; the objects stay
in MinIO and can be re-registered from their keys.

Revision ID: 0005
Revises: 0004
Create Date: 2026-11-19
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "landed_files",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("dataset_id", sa.Uuid(), nullable=False),
        sa.Column("contract_version", sa.Integer(), nullable=False),
        sa.Column("business_date", sa.Date(), nullable=False),
        sa.Column("original_filename", sa.String(length=255), nullable=False),
        sa.Column("object_key", sa.String(length=512), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("record_count", sa.Integer(), nullable=False),
        sa.Column("landed_by_user_id", sa.Uuid(), nullable=True),
        sa.Column(
            "landed_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint("length(sha256) = 64", name="sha256_length"),
        sa.CheckConstraint("size_bytes > 0", name="size_positive"),
        sa.CheckConstraint("record_count >= 0", name="record_count_nonnegative"),
        sa.CheckConstraint("contract_version >= 1", name="contract_version_positive"),
        sa.CheckConstraint(
            "length(original_filename) BETWEEN 1 AND 255", name="filename_length"
        ),
        sa.ForeignKeyConstraint(
            ["dataset_id"],
            ["datasets.id"],
            name="fk_landed_files_dataset_id_datasets",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["landed_by_user_id"],
            ["users.id"],
            name="fk_landed_files_landed_by_user_id_users",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_landed_files"),
        sa.UniqueConstraint("dataset_id", "sha256", name="uq_landed_files_dataset_id"),
        sa.UniqueConstraint("object_key", name="uq_landed_files_object_key"),
    )


def downgrade() -> None:
    op.drop_table("landed_files")
