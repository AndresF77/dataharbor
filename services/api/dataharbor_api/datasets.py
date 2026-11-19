"""Dataset registry: `/v1/workspaces/{workspace_id}/datasets`.

Viewers read the registry; engineers register and edit datasets. Updates
carry the `version` the caller last read and fail with 409 if someone else
changed the dataset in between.
"""

import base64
import binascii
import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from dataharbor_api.auth import WorkspaceAccess, get_session, require_role
from dataharbor_api.models import Dataset, Layer, Role, WorkspaceMember

router = APIRouter(prefix="/v1/workspaces/{workspace_id}/datasets", tags=["datasets"])

NAME_PATTERN = r"^[a-z][a-z0-9_]{1,62}$"
MAX_SLA_MINUTES = 525_600  # one year


class DatasetCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(pattern=NAME_PATTERN, description="Lowercase identifier, e.g. `stg_orders`")
    layer: Layer
    description: str = Field(default="", max_length=2000)
    owner_user_id: uuid.UUID | None = None
    freshness_sla_minutes: int | None = Field(default=None, ge=1, le=MAX_SLA_MINUTES)


class DatasetUpdate(BaseModel):
    """Partial update. Omitted fields stay unchanged; `null` clears owner or SLA."""

    model_config = ConfigDict(extra="forbid")

    version: int = Field(ge=1, description="The version this edit is based on")
    layer: Layer | None = None
    description: str | None = Field(default=None, max_length=2000)
    owner_user_id: uuid.UUID | None = None
    freshness_sla_minutes: int | None = Field(default=None, ge=1, le=MAX_SLA_MINUTES)


class DatasetOut(BaseModel):
    id: uuid.UUID
    name: str
    layer: Layer
    description: str
    owner_user_id: uuid.UUID | None
    freshness_sla_minutes: int | None
    version: int
    created_at: datetime
    updated_at: datetime


class DatasetPage(BaseModel):
    items: list[DatasetOut]
    next_cursor: str | None


def _out(dataset: Dataset) -> DatasetOut:
    return DatasetOut(
        id=dataset.id,
        name=dataset.name,
        layer=Layer(dataset.layer),
        description=dataset.description,
        owner_user_id=dataset.owner_user_id,
        freshness_sla_minutes=dataset.freshness_sla_minutes,
        version=dataset.version,
        created_at=dataset.created_at,
        updated_at=dataset.updated_at,
    )


def encode_cursor(name: str) -> str:
    return base64.urlsafe_b64encode(name.encode()).decode().rstrip("=")


def decode_cursor(cursor: str) -> str:
    try:
        name = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)).decode()
    except (binascii.Error, UnicodeDecodeError, ValueError):
        raise HTTPException(422, "Invalid cursor") from None
    if not name or len(name) > 63:
        raise HTTPException(422, "Invalid cursor")
    return name


def _check_owner(session: Session, workspace_id: uuid.UUID, owner_id: uuid.UUID | None) -> None:
    if owner_id is None:
        return
    if session.get(WorkspaceMember, (workspace_id, owner_id)) is None:
        raise HTTPException(422, "The owner must be a member of this workspace")


def _dataset(
    session: Session, workspace_id: uuid.UUID, dataset_id: uuid.UUID, *, lock: bool = False
) -> Dataset:
    query = select(Dataset).where(Dataset.id == dataset_id, Dataset.workspace_id == workspace_id)
    if lock:
        query = query.with_for_update()
    dataset = session.scalar(query)
    if dataset is None:
        raise HTTPException(404, "Dataset not found")
    return dataset


@router.get("", response_model=DatasetPage)
def list_datasets(
    access: WorkspaceAccess = Depends(require_role(Role.VIEWER)),
    session: Session = Depends(get_session),
    layer: Layer | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    cursor: str | None = Query(default=None, max_length=128),
) -> DatasetPage:
    """Datasets ordered by name, `limit` per page; pass `next_cursor` back for more."""
    query = select(Dataset).where(Dataset.workspace_id == access.workspace.id)
    if layer is not None:
        query = query.where(Dataset.layer == layer.value)
    if cursor is not None:
        query = query.where(Dataset.name > decode_cursor(cursor))
    rows = session.scalars(query.order_by(Dataset.name).limit(limit + 1)).all()
    items = rows[:limit]
    next_cursor = encode_cursor(items[-1].name) if len(rows) > limit else None
    return DatasetPage(items=[_out(d) for d in items], next_cursor=next_cursor)


@router.post("", response_model=DatasetOut, status_code=201)
def create_dataset(
    body: DatasetCreate,
    access: WorkspaceAccess = Depends(require_role(Role.ENGINEER)),
    session: Session = Depends(get_session),
) -> DatasetOut:
    workspace_id = access.workspace.id
    _check_owner(session, workspace_id, body.owner_user_id)
    dataset = Dataset(
        workspace_id=workspace_id,
        name=body.name,
        layer=body.layer.value,
        description=body.description,
        owner_user_id=body.owner_user_id,
        freshness_sla_minutes=body.freshness_sla_minutes,
        version=1,
    )
    session.add(dataset)
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        raise HTTPException(409, "A dataset with this name already exists") from None
    session.refresh(dataset)
    return _out(dataset)


@router.get("/{dataset_id}", response_model=DatasetOut)
def get_dataset(
    dataset_id: uuid.UUID,
    access: WorkspaceAccess = Depends(require_role(Role.VIEWER)),
    session: Session = Depends(get_session),
) -> DatasetOut:
    return _out(_dataset(session, access.workspace.id, dataset_id))


@router.patch("/{dataset_id}", response_model=DatasetOut)
def update_dataset(
    dataset_id: uuid.UUID,
    body: DatasetUpdate,
    access: WorkspaceAccess = Depends(require_role(Role.ENGINEER)),
    session: Session = Depends(get_session),
) -> DatasetOut:
    workspace_id = access.workspace.id
    dataset = _dataset(session, workspace_id, dataset_id, lock=True)
    if body.version != dataset.version:
        raise HTTPException(
            409, f"Dataset was changed by someone else (current version {dataset.version})"
        )

    changes = body.model_dump(exclude_unset=True, exclude={"version"})
    if changes.get("layer", ...) is None or changes.get("description", ...) is None:
        raise HTTPException(422, "layer and description cannot be null")
    if "owner_user_id" in changes:
        _check_owner(session, workspace_id, changes["owner_user_id"])
    if "layer" in changes:
        changes["layer"] = changes["layer"].value
    for field, value in changes.items():
        setattr(dataset, field, value)
    dataset.version += 1
    session.commit()
    session.refresh(dataset)
    return _out(dataset)
