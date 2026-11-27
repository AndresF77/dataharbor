"""Data quality rules on raw datasets and their results on landed files.

Routes (viewers read, engineers change and run):

- `GET|POST /v1/workspaces/{ws}/datasets/{id}/quality-rules` — list by name
  or create a rule (raw datasets with a contract only; checked against the
  latest contract; `409` on a taken name).
- `GET|PUT|DELETE …/quality-rules/{rule_id}` — `PUT` replaces the rule's
  check, severity, description and `enabled` and needs the current
  `version` (`409` otherwise). Deleting keeps past results.
- `POST …/datasets/{id}/files/{file_id}/quality-checks` — run every enabled
  rule over the stored file (its checksum is verified first) and record one
  result per rule version. Re-running returns the recorded results.
- `GET …/datasets/{id}/quality-results`, `GET /v1/workspaces/{ws}/quality-results`
  — result history, newest first, with filters.

A file's quality status is `failed` when an `error` rule fails or cannot
run, `warning` when only `warn` rules do, `passed` otherwise and `no_rules`
when no rule is enabled.
"""

import enum
import logging
import uuid
from datetime import date, datetime

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from dataharbor_api.auth import WorkspaceAccess, get_session, require_role
from dataharbor_api.contracts import find_contract, spec_of
from dataharbor_api.datasets import load_dataset
from dataharbor_api.freshness import as_utc
from dataharbor_api.landing import get_object_store
from dataharbor_api.models import (
    Dataset,
    IngestionContract,
    LandedFile,
    Layer,
    QualityResult,
    QualityRule,
    ResultStatus,
    Role,
    Severity,
)
from dataharbor_api.quality_checks import (
    CheckKind,
    CheckOutcome,
    CheckSpec,
    QualityInputError,
    run_checks,
)
from dataharbor_api.storage import ObjectStore, StorageError, sha256_hex

logger = logging.getLogger(__name__)

dataset_router = APIRouter(
    prefix="/v1/workspaces/{workspace_id}/datasets/{dataset_id}", tags=["data quality"]
)
workspace_router = APIRouter(prefix="/v1/workspaces/{workspace_id}", tags=["data quality"])

RULE_NAME_PATTERN = r"^[a-z][a-z0-9_]{1,62}$"


class RuleBody(CheckSpec):
    severity: Severity = Severity.ERROR
    description: str = Field(default="", max_length=500)
    enabled: bool = True


class RuleCreate(RuleBody):
    name: str = Field(pattern=RULE_NAME_PATTERN)


class RuleReplace(RuleBody):
    version: int = Field(ge=1, description="The version you read")


class RuleOut(BaseModel):
    id: uuid.UUID
    dataset_id: uuid.UUID
    name: str
    check: CheckKind
    columns: list[str]
    values: list[str] | None
    min: str | None
    max: str | None
    severity: Severity
    description: str
    enabled: bool
    version: int
    updated_by_user_id: uuid.UUID | None
    created_at: datetime
    updated_at: datetime


class RuleList(BaseModel):
    items: list[RuleOut]


class ResultOut(BaseModel):
    id: uuid.UUID
    dataset_id: uuid.UUID
    landed_file_id: uuid.UUID
    rule_id: uuid.UUID | None = Field(description="Null once the rule is deleted")
    rule_name: str
    rule_version: int
    check: CheckKind
    severity: Severity
    status: ResultStatus
    checked_records: int
    failing_records: int
    sample_records: list[int] = Field(description="Up to 20 failing record numbers (1-based)")
    message: str
    evaluated_by_user_id: uuid.UUID | None
    evaluated_at: datetime


class FileQuality(str, enum.Enum):
    FAILED = "failed"
    WARNING = "warning"
    PASSED = "passed"
    NO_RULES = "no_rules"


class CheckRunOut(BaseModel):
    landed_file_id: uuid.UUID
    business_date: date
    contract_version: int
    record_count: int
    status: FileQuality
    counts: dict[ResultStatus, int]
    results: list[ResultOut]


class ResultPage(BaseModel):
    items: list[ResultOut]


def _spec(rule: QualityRule) -> CheckSpec:
    return CheckSpec(check=CheckKind(rule.check_kind), columns=rule.columns, **rule.params)


def _rule_out(rule: QualityRule) -> RuleOut:
    spec = _spec(rule)
    return RuleOut(
        id=rule.id,
        dataset_id=rule.dataset_id,
        name=rule.name,
        check=spec.check,
        columns=spec.columns,
        values=spec.values,
        min=spec.min,
        max=spec.max,
        severity=Severity(rule.severity),
        description=rule.description,
        enabled=rule.enabled,
        version=rule.version,
        updated_by_user_id=rule.updated_by_user_id,
        created_at=rule.created_at,
        updated_at=rule.updated_at,
    )


def _result_out(row: QualityResult) -> ResultOut:
    return ResultOut(
        id=row.id,
        dataset_id=row.dataset_id,
        landed_file_id=row.landed_file_id,
        rule_id=row.rule_id,
        rule_name=row.rule_name,
        rule_version=row.rule_version,
        check=CheckKind(row.check_kind),
        severity=Severity(row.severity),
        status=ResultStatus(row.status),
        checked_records=row.checked_records,
        failing_records=row.failing_records,
        sample_records=row.sample_records,
        message=row.message,
        evaluated_by_user_id=row.evaluated_by_user_id,
        evaluated_at=as_utc(row.evaluated_at),
    )


def _quality_dataset(session: Session, access: WorkspaceAccess, dataset_id: uuid.UUID,
                     *, lock: bool = False) -> Dataset:
    dataset = load_dataset(session, access.workspace.id, dataset_id, lock=lock)
    if dataset.layer != Layer.RAW.value:
        raise HTTPException(
            422, "Quality rules check landed files; only raw datasets have them"
        )
    return dataset


def _check_against_latest_contract(session: Session, dataset: Dataset, spec: CheckSpec) -> None:
    try:
        contract = find_contract(session, dataset)
    except HTTPException:
        raise HTTPException(
            422, "Publish an ingestion contract before adding quality rules"
        ) from None
    problems = spec.problems_with(spec_of(contract))
    if problems:
        raise HTTPException(422, "; ".join(problems))


def _apply(rule: QualityRule, body: RuleBody, user_id: uuid.UUID) -> None:
    rule.check_kind = body.check.value
    rule.columns = list(body.columns)
    rule.params = {
        key: value
        for key, value in (("values", body.values), ("min", body.min), ("max", body.max))
        if value is not None
    }
    rule.severity = body.severity.value
    rule.description = body.description
    rule.enabled = body.enabled
    rule.updated_by_user_id = user_id


def _load_rule(session: Session, dataset: Dataset, rule_id: uuid.UUID,
               *, lock: bool = False) -> QualityRule:
    query = select(QualityRule).where(
        QualityRule.id == rule_id, QualityRule.dataset_id == dataset.id
    )
    if lock:
        query = query.with_for_update()
    rule = session.scalar(query)
    if rule is None:
        raise HTTPException(404, "Quality rule not found")
    return rule


@dataset_router.get("/quality-rules", response_model=RuleList)
def list_rules(
    dataset_id: uuid.UUID,
    access: WorkspaceAccess = Depends(require_role(Role.VIEWER)),
    session: Session = Depends(get_session),
) -> RuleList:
    dataset = load_dataset(session, access.workspace.id, dataset_id)
    rules = session.scalars(
        select(QualityRule).where(QualityRule.dataset_id == dataset.id).order_by(QualityRule.name)
    ).all()
    return RuleList(items=[_rule_out(r) for r in rules])


@dataset_router.post("/quality-rules", response_model=RuleOut, status_code=201)
def create_rule(
    dataset_id: uuid.UUID,
    body: RuleCreate,
    access: WorkspaceAccess = Depends(require_role(Role.ENGINEER)),
    session: Session = Depends(get_session),
) -> RuleOut:
    dataset = _quality_dataset(session, access, dataset_id)
    _check_against_latest_contract(session, dataset, body)
    rule = QualityRule(dataset_id=dataset.id, name=body.name, version=1)
    _apply(rule, body, access.user.id)
    session.add(rule)
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        raise HTTPException(409, f"This dataset already has a rule named {body.name}") from None
    session.refresh(rule)
    logger.info("quality rule created dataset=%s rule=%s check=%s",
                dataset.id, rule.id, rule.check_kind)
    return _rule_out(rule)


@dataset_router.get("/quality-rules/{rule_id}", response_model=RuleOut)
def get_rule(
    dataset_id: uuid.UUID,
    rule_id: uuid.UUID,
    access: WorkspaceAccess = Depends(require_role(Role.VIEWER)),
    session: Session = Depends(get_session),
) -> RuleOut:
    dataset = load_dataset(session, access.workspace.id, dataset_id)
    return _rule_out(_load_rule(session, dataset, rule_id))


@dataset_router.put("/quality-rules/{rule_id}", response_model=RuleOut)
def replace_rule(
    dataset_id: uuid.UUID,
    rule_id: uuid.UUID,
    body: RuleReplace,
    access: WorkspaceAccess = Depends(require_role(Role.ENGINEER)),
    session: Session = Depends(get_session),
) -> RuleOut:
    dataset = _quality_dataset(session, access, dataset_id)
    rule = _load_rule(session, dataset, rule_id, lock=True)
    if body.version != rule.version:
        raise HTTPException(
            409, f"Rule was changed by someone else (current version {rule.version})"
        )
    _check_against_latest_contract(session, dataset, body)
    _apply(rule, body, access.user.id)
    rule.version += 1
    session.commit()
    session.refresh(rule)
    logger.info("quality rule replaced dataset=%s rule=%s version=%s",
                dataset.id, rule.id, rule.version)
    return _rule_out(rule)


@dataset_router.delete("/quality-rules/{rule_id}", status_code=204)
def delete_rule(
    dataset_id: uuid.UUID,
    rule_id: uuid.UUID,
    access: WorkspaceAccess = Depends(require_role(Role.ENGINEER)),
    session: Session = Depends(get_session),
) -> Response:
    dataset = load_dataset(session, access.workspace.id, dataset_id)
    rule = _load_rule(session, dataset, rule_id, lock=True)
    # Same as the FK's ON DELETE SET NULL, done here so it holds without FK enforcement too.
    session.execute(
        update(QualityResult).where(QualityResult.rule_id == rule.id).values(rule_id=None)
    )
    session.delete(rule)
    session.commit()
    logger.info("quality rule deleted dataset=%s rule=%s", dataset.id, rule_id)
    return Response(status_code=204)


def file_quality(results: list[ResultOut]) -> FileQuality:
    if not results:
        return FileQuality.NO_RULES
    failing = [r for r in results if r.status is not ResultStatus.PASS]
    if any(r.severity is Severity.ERROR for r in failing):
        return FileQuality.FAILED
    return FileQuality.WARNING if failing else FileQuality.PASSED


def _new_result(rule: QualityRule, landed: LandedFile, outcome: CheckOutcome | None,
                error: str | None, user_id: uuid.UUID) -> QualityResult:
    if outcome is None:
        status, checked, failing, sample, message = ResultStatus.ERROR, 0, 0, [], error or ""
    else:
        status = ResultStatus.PASS if outcome.passed else ResultStatus.FAIL
        checked, failing = outcome.checked_records, outcome.failing_records
        sample, message = outcome.sample_records, outcome.message
    return QualityResult(
        dataset_id=landed.dataset_id,
        landed_file_id=landed.id,
        rule_id=rule.id,
        rule_name=rule.name,
        rule_version=rule.version,
        check_kind=rule.check_kind,
        severity=rule.severity,
        status=status.value,
        checked_records=checked,
        failing_records=failing,
        sample_records=sample,
        message=message[:500],
        evaluated_by_user_id=user_id,
    )


def _recorded(session: Session, landed: LandedFile, rules: list[QualityRule]) -> dict:
    if not rules:
        return {}
    rows = session.scalars(
        select(QualityResult).where(
            QualityResult.landed_file_id == landed.id,
            QualityResult.rule_id.in_([r.id for r in rules]),
        )
    ).all()
    return {(row.rule_id, row.rule_version): row for row in rows}


@dataset_router.post("/files/{file_id}/quality-checks", response_model=CheckRunOut)
def run_quality_checks(
    dataset_id: uuid.UUID,
    file_id: uuid.UUID,
    response: Response,
    access: WorkspaceAccess = Depends(require_role(Role.ENGINEER)),
    session: Session = Depends(get_session),
    store: ObjectStore = Depends(get_object_store),
) -> CheckRunOut:
    """Evaluate the enabled rules on one landed file; `201` if any result is new."""
    dataset = _quality_dataset(session, access, dataset_id)
    landed = session.scalar(
        select(LandedFile).where(LandedFile.id == file_id, LandedFile.dataset_id == dataset.id)
    )
    if landed is None:
        raise HTTPException(404, "Landed file not found")
    rules = list(session.scalars(
        select(QualityRule)
        .where(QualityRule.dataset_id == dataset.id, QualityRule.enabled.is_(True))
        .order_by(QualityRule.name)
    ).all())

    recorded = _recorded(session, landed, rules)
    pending = [r for r in rules if (r.id, r.version) not in recorded]
    if pending:
        contract = session.scalar(
            select(IngestionContract).where(
                IngestionContract.dataset_id == dataset.id,
                IngestionContract.version == landed.contract_version,
            )
        )
        spec = spec_of(contract)
        try:
            data = store.get(landed.object_key)
        except StorageError:
            raise HTTPException(503, "Landing storage is unavailable; try again later") from None
        if sha256_hex(data) != landed.sha256:
            logger.warning("quality checks refused, checksum mismatch dataset=%s file=%s",
                           dataset.id, landed.id)
            raise HTTPException(409, "The stored file no longer matches its checksum")

        runnable, errors = [], {}
        for rule in pending:
            try:
                check = _spec(rule)
            except ValidationError:
                errors[rule.id] = "The rule's stored definition is invalid"
                continue
            problems = check.problems_with(spec)
            if problems:
                errors[rule.id] = (
                    f"Cannot run on contract version {landed.contract_version}: "
                    + "; ".join(problems)
                )
            else:
                runnable.append((rule, check))
        try:
            _, outcomes = run_checks(spec, data, [check for _, check in runnable])
        except QualityInputError as exc:
            outcomes = None
            for rule, _ in runnable:
                errors[rule.id] = str(exc)
            runnable = []
        by_rule = {rule.id: outcome for (rule, _), outcome in zip(runnable, outcomes or [])}
        for rule in pending:
            session.add(_new_result(rule, landed, by_rule.get(rule.id), errors.get(rule.id),
                                    access.user.id))
        try:
            session.commit()
            response.status_code = 201
        except IntegrityError:
            # Someone recorded the same rule versions concurrently; theirs stand.
            session.rollback()
        recorded = _recorded(session, landed, rules)

    results = [_result_out(recorded[(r.id, r.version)]) for r in rules
               if (r.id, r.version) in recorded]
    counts = {s: 0 for s in ResultStatus}
    for result in results:
        counts[result.status] += 1
    status = file_quality(results)
    logger.info(
        "quality checks dataset=%s file=%s status=%s pass=%d fail=%d error=%d",
        dataset.id, landed.id, status.value,
        counts[ResultStatus.PASS], counts[ResultStatus.FAIL], counts[ResultStatus.ERROR],
    )
    return CheckRunOut(
        landed_file_id=landed.id,
        business_date=landed.business_date,
        contract_version=landed.contract_version,
        record_count=landed.record_count,
        status=status,
        counts=counts,
        results=results,
    )


def _history(
    session: Session,
    workspace_id: uuid.UUID,
    *,
    dataset_id: uuid.UUID | None,
    status: list[ResultStatus] | None,
    severity: Severity | None,
    rule_id: uuid.UUID | None,
    landed_file_id: uuid.UUID | None,
    business_date: date | None,
    limit: int,
) -> ResultPage:
    query = (
        select(QualityResult)
        .join(Dataset, Dataset.id == QualityResult.dataset_id)
        .where(Dataset.workspace_id == workspace_id)
    )
    if dataset_id is not None:
        query = query.where(QualityResult.dataset_id == dataset_id)
    if status:
        query = query.where(QualityResult.status.in_([s.value for s in status]))
    if severity is not None:
        query = query.where(QualityResult.severity == severity.value)
    if rule_id is not None:
        query = query.where(QualityResult.rule_id == rule_id)
    if landed_file_id is not None:
        query = query.where(QualityResult.landed_file_id == landed_file_id)
    if business_date is not None:
        query = query.join(LandedFile, LandedFile.id == QualityResult.landed_file_id).where(
            LandedFile.business_date == business_date
        )
    rows = session.scalars(
        query.order_by(
            QualityResult.evaluated_at.desc(), QualityResult.rule_name, QualityResult.id
        ).limit(limit)
    ).all()
    return ResultPage(items=[_result_out(r) for r in rows])


@dataset_router.get("/quality-results", response_model=ResultPage)
def dataset_results(
    dataset_id: uuid.UUID,
    access: WorkspaceAccess = Depends(require_role(Role.VIEWER)),
    session: Session = Depends(get_session),
    status: list[ResultStatus] | None = Query(default=None),
    severity: Severity | None = None,
    rule_id: uuid.UUID | None = None,
    landed_file_id: uuid.UUID | None = None,
    business_date: date | None = None,
    limit: int = Query(default=50, ge=1, le=200),
) -> ResultPage:
    """One dataset's results, newest first. `status` is repeatable."""
    dataset = load_dataset(session, access.workspace.id, dataset_id)
    return _history(
        session, access.workspace.id, dataset_id=dataset.id, status=status, severity=severity,
        rule_id=rule_id, landed_file_id=landed_file_id, business_date=business_date, limit=limit,
    )


@workspace_router.get("/quality-results", response_model=ResultPage)
def workspace_results(
    access: WorkspaceAccess = Depends(require_role(Role.VIEWER)),
    session: Session = Depends(get_session),
    dataset_id: uuid.UUID | None = None,
    status: list[ResultStatus] | None = Query(default=None),
    severity: Severity | None = None,
    business_date: date | None = None,
    limit: int = Query(default=50, ge=1, le=200),
) -> ResultPage:
    """Results across the workspace's datasets, newest first."""
    return _history(
        session, access.workspace.id, dataset_id=dataset_id, status=status, severity=severity,
        rule_id=None, landed_file_id=None, business_date=business_date, limit=limit,
    )
