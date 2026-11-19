"""Landing zone: `/v1/workspaces/{workspace_id}/datasets/{dataset_id}/files`.

An engineer sends a source file for a raw dataset as the raw request body.
The file is checked against the dataset's latest ingestion contract; only
a passing file is written to the landing bucket, under a key derived from
its SHA-256:

    workspaces/<workspace id>/<dataset name>/business_date=<YYYY-MM-DD>/<sha256>.<csv|jsonl>

and recorded in `landed_files`. Sending the same bytes again returns the
existing record (`duplicate: true`) instead of storing a second copy.
Logs carry ids, sizes and checksums only, never file contents.
"""

import logging
import re
import uuid
from datetime import date, datetime

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from dataharbor_api.auth import WorkspaceAccess, get_session, require_role
from dataharbor_api.contract_rules import validate_content
from dataharbor_api.contracts import ViolationOut, find_contract, read_body, spec_of
from dataharbor_api.datasets import load_dataset
from dataharbor_api.errors import error_body
from dataharbor_api.models import Dataset, LandedFile, Layer, Role
from dataharbor_api.storage import ObjectStore, StorageError, sha256_hex

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/v1/workspaces/{workspace_id}/datasets/{dataset_id}/files",
    tags=["landing zone"],
)

FILENAME_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,254}$"
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
CONTENT_TYPES = {"csv": "text/csv", "jsonl": "application/x-ndjson"}


class LandedFileOut(BaseModel):
    id: uuid.UUID
    dataset_id: uuid.UUID
    contract_version: int
    business_date: date
    original_filename: str
    object_key: str
    sha256: str
    size_bytes: int
    record_count: int
    landed_by_user_id: uuid.UUID | None
    landed_at: datetime


class LandingResult(LandedFileOut):
    duplicate: bool = Field(description="True when these exact bytes had already landed")


class LandedFilePage(BaseModel):
    items: list[LandedFileOut]


class VerifyOut(BaseModel):
    intact: bool
    expected_sha256: str
    actual_sha256: str | None = Field(description="`null` when the object is missing")


def get_object_store(request: Request) -> ObjectStore:
    return request.app.state.object_store


async def read_landing_file(request: Request) -> bytes:
    return await read_body(request, request.app.state.settings.max_landing_bytes, "landing file")


def object_key(workspace_id: uuid.UUID, dataset: Dataset, business_date: date, sha256: str,
               fmt: str) -> str:
    return (
        f"workspaces/{workspace_id}/{dataset.name}/"
        f"business_date={business_date.isoformat()}/{sha256}.{fmt}"
    )


def _out(row: LandedFile, duplicate: bool | None = None) -> LandedFileOut:
    fields = {name: getattr(row, name) for name in LandedFileOut.model_fields}
    if duplicate is None:
        return LandedFileOut(**fields)
    return LandingResult(**fields, duplicate=duplicate)


def _existing(session: Session, dataset: Dataset, sha256: str) -> LandedFile | None:
    return session.scalar(
        select(LandedFile).where(LandedFile.dataset_id == dataset.id, LandedFile.sha256 == sha256)
    )


def _load_file(session: Session, dataset: Dataset, file_id: uuid.UUID) -> LandedFile:
    row = session.scalar(
        select(LandedFile).where(LandedFile.id == file_id, LandedFile.dataset_id == dataset.id)
    )
    if row is None:
        raise HTTPException(404, "Landed file not found")
    return row


@router.post("", response_model=LandingResult, status_code=201)
def land_file(
    request: Request,
    response: Response,
    dataset_id: uuid.UUID,
    business_date: date = Query(description="Business date the file covers (YYYY-MM-DD)"),
    filename: str = Query(pattern=FILENAME_PATTERN, description="Original file name"),
    content_sha256: str | None = Header(
        default=None,
        alias="X-Content-SHA256",
        description="Optional hex SHA-256 of the body; the upload is refused if it differs",
    ),
    access: WorkspaceAccess = Depends(require_role(Role.ENGINEER)),
    data: bytes = Depends(read_landing_file),
    session: Session = Depends(get_session),
    store: ObjectStore = Depends(get_object_store),
):
    dataset = load_dataset(session, access.workspace.id, dataset_id)
    if dataset.layer != Layer.RAW.value:
        raise HTTPException(422, "Only raw datasets have a landing zone")

    sha256 = sha256_hex(data)
    if content_sha256 is not None:
        claimed = content_sha256.strip().lower()
        if not SHA256_PATTERN.fullmatch(claimed):
            raise HTTPException(422, "X-Content-SHA256 must be 64 hexadecimal characters")
        if claimed != sha256:
            raise HTTPException(422, "Body does not match X-Content-SHA256")

    existing = _existing(session, dataset, sha256)
    if existing is not None:
        response.status_code = 200
        return _out(existing, duplicate=True)

    try:
        contract = find_contract(session, dataset)
    except HTTPException:
        raise HTTPException(
            422, "Publish an ingestion contract before landing files"
        ) from None
    report = validate_content(spec_of(contract), data)
    if not report.valid:
        logger.info(
            "landing rejected dataset=%s sha256=%s violations=%d",
            dataset.id, sha256, report.violation_count,
        )
        details = {
            "contract_version": contract.version,
            "record_count": report.record_count,
            "violation_count": report.violation_count,
            "violations": [
                ViolationOut(
                    rule=v.rule.value, message=v.message, column=v.column, record=v.record
                ).model_dump()
                for v in report.violations
            ],
            "truncated": report.truncated,
        }
        return JSONResponse(
            error_body(request, 422, "File does not match the ingestion contract", details),
            status_code=422,
        )

    key = object_key(access.workspace.id, dataset, business_date, sha256, contract.format)
    try:
        store.put(key, data, content_type=CONTENT_TYPES[contract.format], sha256=sha256)
    except StorageError as exc:
        logger.warning("landing storage failed dataset=%s sha256=%s error=%s",
                       dataset.id, sha256, exc)
        raise HTTPException(503, "Landing storage is unavailable; try again later") from None

    row = LandedFile(
        dataset_id=dataset.id,
        contract_version=contract.version,
        business_date=business_date,
        original_filename=filename,
        object_key=key,
        sha256=sha256,
        size_bytes=len(data),
        record_count=report.record_count,
        landed_by_user_id=access.user.id,
    )
    session.add(row)
    try:
        session.commit()
    except IntegrityError:
        # Same bytes landed concurrently; the object key is identical, so keep theirs.
        session.rollback()
        existing = _existing(session, dataset, sha256)
        if existing is None:
            raise
        response.status_code = 200
        return _out(existing, duplicate=True)
    session.refresh(row)
    logger.info(
        "landed dataset=%s file=%s sha256=%s bytes=%d records=%d",
        dataset.id, row.id, sha256, row.size_bytes, row.record_count,
    )
    return _out(row, duplicate=False)


@router.get("", response_model=LandedFilePage)
def list_landed_files(
    dataset_id: uuid.UUID,
    access: WorkspaceAccess = Depends(require_role(Role.VIEWER)),
    session: Session = Depends(get_session),
    business_date: date | None = None,
    limit: int = Query(default=50, ge=1, le=200),
) -> LandedFilePage:
    """Landed files, newest business date first."""
    dataset = load_dataset(session, access.workspace.id, dataset_id)
    query = select(LandedFile).where(LandedFile.dataset_id == dataset.id)
    if business_date is not None:
        query = query.where(LandedFile.business_date == business_date)
    rows = session.scalars(
        query.order_by(
            LandedFile.business_date.desc(), LandedFile.landed_at.desc(), LandedFile.id
        ).limit(limit)
    ).all()
    return LandedFilePage(items=[_out(r) for r in rows])


@router.get("/{file_id}", response_model=LandedFileOut)
def get_landed_file(
    dataset_id: uuid.UUID,
    file_id: uuid.UUID,
    access: WorkspaceAccess = Depends(require_role(Role.VIEWER)),
    session: Session = Depends(get_session),
) -> LandedFileOut:
    dataset = load_dataset(session, access.workspace.id, dataset_id)
    return _out(_load_file(session, dataset, file_id))


@router.post("/{file_id}/verify", response_model=VerifyOut)
def verify_landed_file(
    dataset_id: uuid.UUID,
    file_id: uuid.UUID,
    access: WorkspaceAccess = Depends(require_role(Role.ENGINEER)),
    session: Session = Depends(get_session),
    store: ObjectStore = Depends(get_object_store),
) -> VerifyOut:
    """Re-read the stored object and compare its SHA-256 with the recorded one."""
    dataset = load_dataset(session, access.workspace.id, dataset_id)
    row = _load_file(session, dataset, file_id)
    try:
        present = store.head(row.object_key)
        actual = sha256_hex(store.get(row.object_key)) if present is not None else None
    except StorageError:
        raise HTTPException(503, "Landing storage is unavailable; try again later") from None
    intact = actual == row.sha256
    if not intact:
        logger.warning("landed file checksum mismatch dataset=%s file=%s", dataset.id, row.id)
    return VerifyOut(intact=intact, expected_sha256=row.sha256, actual_sha256=actual)
