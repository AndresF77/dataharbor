"""Ingestion contracts: `/v1/workspaces/{workspace_id}/datasets/{dataset_id}/contract`.

Only raw datasets have a contract. Publishing never edits a version in
place: it adds `latest + 1`, and the caller must send the version it was
based on (`null` for the first) so two engineers cannot overwrite each
other. Any member can check a sample file against a contract version.
"""

import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from dataharbor_api.auth import WorkspaceAccess, get_session, require_role
from dataharbor_api.contract_rules import (
    ContractColumn,
    ContractSpec,
    SourceFormat,
    validate_content,
)
from dataharbor_api.datasets import load_dataset
from dataharbor_api.models import Dataset, IngestionContract, Layer, Role

router = APIRouter(
    prefix="/v1/workspaces/{workspace_id}/datasets/{dataset_id}/contract",
    tags=["ingestion contracts"],
)

MAX_SAMPLE_BYTES = 5 * 1024 * 1024


class ContractPublish(ContractSpec):
    model_config = ConfigDict(extra="forbid")

    based_on_version: int | None = Field(
        ge=1, description="Latest version this edit starts from; `null` for the first contract"
    )


class ContractOut(BaseModel):
    id: uuid.UUID
    dataset_id: uuid.UUID
    version: int
    format: SourceFormat
    columns: list[ContractColumn]
    primary_key: list[str]
    created_by_user_id: uuid.UUID | None
    created_at: datetime


class ContractVersions(BaseModel):
    items: list[ContractOut]


class ViolationOut(BaseModel):
    rule: str
    message: str
    column: str | None
    record: int | None


class ValidationOut(BaseModel):
    valid: bool
    contract_version: int
    record_count: int
    violation_count: int
    violations: list[ViolationOut]
    truncated: bool = Field(description="True when more violations exist than are listed")


def _out(contract: IngestionContract) -> ContractOut:
    return ContractOut(
        id=contract.id,
        dataset_id=contract.dataset_id,
        version=contract.version,
        format=SourceFormat(contract.format),
        columns=[ContractColumn.model_validate(c) for c in contract.columns],
        primary_key=list(contract.primary_key),
        created_by_user_id=contract.created_by_user_id,
        created_at=contract.created_at,
    )


def spec_of(contract: IngestionContract) -> ContractSpec:
    return ContractSpec(
        format=SourceFormat(contract.format),
        columns=contract.columns,
        primary_key=contract.primary_key,
    )


def _contract(
    session: Session, dataset: Dataset, version: int | None = None
) -> IngestionContract:
    query = select(IngestionContract).where(IngestionContract.dataset_id == dataset.id)
    if version is None:
        query = query.order_by(IngestionContract.version.desc()).limit(1)
    else:
        query = query.where(IngestionContract.version == version)
    contract = session.scalar(query)
    if contract is None:
        message = "No ingestion contract yet" if version is None else "Contract version not found"
        raise HTTPException(404, message)
    return contract


async def read_sample(request: Request) -> bytes:
    """The raw request body, refused above MAX_SAMPLE_BYTES without reading it all."""
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > MAX_SAMPLE_BYTES:
        raise HTTPException(413, f"Sample files are limited to {MAX_SAMPLE_BYTES} bytes")
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > MAX_SAMPLE_BYTES:
            raise HTTPException(413, f"Sample files are limited to {MAX_SAMPLE_BYTES} bytes")
    if not body:
        raise HTTPException(422, "The sample file is empty")
    return bytes(body)


@router.get("", response_model=ContractOut)
def get_latest_contract(
    dataset_id: uuid.UUID,
    access: WorkspaceAccess = Depends(require_role(Role.VIEWER)),
    session: Session = Depends(get_session),
) -> ContractOut:
    dataset = load_dataset(session, access.workspace.id, dataset_id)
    return _out(_contract(session, dataset))


@router.get("/versions", response_model=ContractVersions)
def list_contract_versions(
    dataset_id: uuid.UUID,
    access: WorkspaceAccess = Depends(require_role(Role.VIEWER)),
    session: Session = Depends(get_session),
) -> ContractVersions:
    """Every published version, newest first."""
    dataset = load_dataset(session, access.workspace.id, dataset_id)
    rows = session.scalars(
        select(IngestionContract)
        .where(IngestionContract.dataset_id == dataset.id)
        .order_by(IngestionContract.version.desc())
    ).all()
    return ContractVersions(items=[_out(c) for c in rows])


@router.get("/versions/{version}", response_model=ContractOut)
def get_contract_version(
    dataset_id: uuid.UUID,
    version: int,
    access: WorkspaceAccess = Depends(require_role(Role.VIEWER)),
    session: Session = Depends(get_session),
) -> ContractOut:
    dataset = load_dataset(session, access.workspace.id, dataset_id)
    return _out(_contract(session, dataset, version))


@router.post("", response_model=ContractOut, status_code=201)
def publish_contract(
    dataset_id: uuid.UUID,
    body: ContractPublish,
    access: WorkspaceAccess = Depends(require_role(Role.ENGINEER)),
    session: Session = Depends(get_session),
) -> ContractOut:
    # Lock the dataset so concurrent publishes are serialised on PostgreSQL.
    dataset = load_dataset(session, access.workspace.id, dataset_id, lock=True)
    if dataset.layer != Layer.RAW.value:
        raise HTTPException(422, "Ingestion contracts apply to raw datasets only")
    latest = session.scalar(
        select(func.max(IngestionContract.version)).where(
            IngestionContract.dataset_id == dataset.id
        )
    )
    if body.based_on_version != latest:
        current = "none" if latest is None else str(latest)
        raise HTTPException(
            409, f"Contract was changed by someone else (latest version {current})"
        )
    contract = IngestionContract(
        dataset_id=dataset.id,
        version=(latest or 0) + 1,
        format=body.format.value,
        columns=[c.model_dump(mode="json") for c in body.columns],
        primary_key=list(body.primary_key),
        created_by_user_id=access.user.id,
    )
    session.add(contract)
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        raise HTTPException(409, "Contract was changed by someone else") from None
    session.refresh(contract)
    return _out(contract)


@router.post("/validate", response_model=ValidationOut)
def validate_sample(
    dataset_id: uuid.UUID,
    access: WorkspaceAccess = Depends(require_role(Role.VIEWER)),
    sample: bytes = Depends(read_sample),
    session: Session = Depends(get_session),
    version: int | None = Query(default=None, ge=1, description="Defaults to the latest"),
) -> ValidationOut:
    """Check a sample file (raw request body) against a contract version. Nothing is stored."""
    dataset = load_dataset(session, access.workspace.id, dataset_id)
    contract = _contract(session, dataset, version)
    report = validate_content(spec_of(contract), sample)
    return ValidationOut(
        valid=report.valid,
        contract_version=contract.version,
        record_count=report.record_count,
        violation_count=report.violation_count,
        violations=[
            ViolationOut(rule=v.rule.value, message=v.message, column=v.column, record=v.record)
            for v in report.violations
        ],
        truncated=report.truncated,
    )
