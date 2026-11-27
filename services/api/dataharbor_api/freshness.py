"""Freshness rules and their evaluation.

A freshness rule says when a dataset counts as updated and how old that
update may get:

- `signal = landed_file`: the newest file landed for the dataset;
- `signal = pipeline_run`: the newest successful run of `pipeline_id`
  (a pipeline of the same workspace), e.g. the transformation that builds a
  staging or mart dataset.

Evaluation compares the age of that update with the rule: `fresh`, then
`warning` once older than `warn_after_minutes` (optional), then `stale` once
older than `error_after_minutes`. A dataset that was never updated is
`unknown`. Thresholds are exclusive: an update exactly `error_after_minutes`
old is still `warning` (or `fresh`).

Routes:

- `GET|PUT|DELETE /v1/workspaces/{ws}/datasets/{id}/freshness-rule` —
  viewers read, engineers create, replace or remove. `PUT` without `version`
  creates the rule (409 if one exists); with `version` it replaces it (409
  if someone else changed it first). `error_after_minutes` defaults to the
  dataset's `freshness_sla_minutes`.
- `GET /v1/workspaces/{ws}/datasets/{id}/freshness` — one dataset's status.
- `GET /v1/workspaces/{ws}/freshness` — every dataset with a rule, worst
  status first, optionally filtered by `status`.
"""

import enum
import logging
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from dataharbor_api.auth import WorkspaceAccess, get_session, require_role
from dataharbor_api.datasets import MAX_SLA_MINUTES, load_dataset
from dataharbor_api.models import (
    Dataset,
    FreshnessRule,
    FreshnessSignal,
    LandedFile,
    Layer,
    Pipeline,
    PipelineRun,
    Role,
    RunStatus,
)

logger = logging.getLogger(__name__)

dataset_router = APIRouter(
    prefix="/v1/workspaces/{workspace_id}/datasets/{dataset_id}", tags=["freshness"]
)
workspace_router = APIRouter(prefix="/v1/workspaces/{workspace_id}", tags=["freshness"])


def utc_now() -> datetime:
    """The evaluation clock. A dependency so tests can pin it."""
    return datetime.now(timezone.utc)


class FreshnessStatus(str, enum.Enum):
    """Ordered from worst to best; lists use this order."""

    STALE = "stale"
    UNKNOWN = "unknown"
    WARNING = "warning"
    FRESH = "fresh"

    @property
    def severity(self) -> int:
        return list(FreshnessStatus).index(self)


@dataclass(frozen=True)
class Thresholds:
    warn_after_minutes: int | None
    error_after_minutes: int


def classify(
    thresholds: Thresholds, last_updated_at: datetime | None, now: datetime
) -> tuple[FreshnessStatus, int | None]:
    """Status and age in whole seconds. Updates "in the future" count as age 0."""
    if last_updated_at is None:
        return FreshnessStatus.UNKNOWN, None
    age = max(0, int((now - as_utc(last_updated_at)).total_seconds()))
    if age > thresholds.error_after_minutes * 60:
        return FreshnessStatus.STALE, age
    if thresholds.warn_after_minutes is not None and age > thresholds.warn_after_minutes * 60:
        return FreshnessStatus.WARNING, age
    return FreshnessStatus.FRESH, age


def as_utc(value: datetime) -> datetime:
    # SQLite hands back naive datetimes; everything is stored in UTC.
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


class RuleWrite(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: int | None = Field(
        default=None, ge=1, description="Omit to create; the version you read to replace"
    )
    signal: FreshnessSignal
    pipeline_id: uuid.UUID | None = Field(
        default=None, description="Required for `pipeline_run`, not allowed otherwise"
    )
    warn_after_minutes: int | None = Field(default=None, ge=1, le=MAX_SLA_MINUTES)
    error_after_minutes: int | None = Field(
        default=None,
        ge=1,
        le=MAX_SLA_MINUTES,
        description="Defaults to the dataset's freshness_sla_minutes",
    )

    @model_validator(mode="after")
    def _pipeline_matches_signal(self) -> "RuleWrite":
        needs_pipeline = self.signal is FreshnessSignal.PIPELINE_RUN
        if needs_pipeline and self.pipeline_id is None:
            raise ValueError("pipeline_id is required for the pipeline_run signal")
        if not needs_pipeline and self.pipeline_id is not None:
            raise ValueError("pipeline_id is only allowed with the pipeline_run signal")
        return self


class RuleOut(BaseModel):
    id: uuid.UUID
    dataset_id: uuid.UUID
    signal: FreshnessSignal
    pipeline_id: uuid.UUID | None
    warn_after_minutes: int | None
    error_after_minutes: int
    version: int
    updated_by_user_id: uuid.UUID | None
    created_at: datetime
    updated_at: datetime


class FreshnessOut(BaseModel):
    dataset_id: uuid.UUID
    dataset_name: str
    layer: Layer
    status: FreshnessStatus
    signal: FreshnessSignal
    pipeline_id: uuid.UUID | None
    warn_after_minutes: int | None
    error_after_minutes: int
    last_updated_at: datetime | None = Field(description="Null until the first update")
    last_business_date: date | None = Field(
        description="Business date of the newest landed file or successful run"
    )
    age_seconds: int | None
    evaluated_at: datetime


class FreshnessList(BaseModel):
    evaluated_at: datetime
    counts: dict[FreshnessStatus, int]
    items: list[FreshnessOut]


def _rule_out(rule: FreshnessRule) -> RuleOut:
    return RuleOut(
        id=rule.id,
        dataset_id=rule.dataset_id,
        signal=FreshnessSignal(rule.signal),
        pipeline_id=rule.pipeline_id,
        warn_after_minutes=rule.warn_after_minutes,
        error_after_minutes=rule.error_after_minutes,
        version=rule.version,
        updated_by_user_id=rule.updated_by_user_id,
        created_at=rule.created_at,
        updated_at=rule.updated_at,
    )


def _load_rule(session: Session, dataset: Dataset, *, lock: bool = False) -> FreshnessRule | None:
    query = select(FreshnessRule).where(FreshnessRule.dataset_id == dataset.id)
    if lock:
        query = query.with_for_update()
    return session.scalar(query)


Update = tuple[datetime | None, date | None]


def _landed_updates(session: Session, dataset_ids: list[uuid.UUID]) -> dict[uuid.UUID, Update]:
    if not dataset_ids:
        return {}
    rows = session.execute(
        select(
            LandedFile.dataset_id,
            func.max(LandedFile.landed_at),
            func.max(LandedFile.business_date),
        )
        .where(LandedFile.dataset_id.in_(dataset_ids))
        .group_by(LandedFile.dataset_id)
    )
    return {dataset_id: (landed_at, day) for dataset_id, landed_at, day in rows}


def _run_updates(session: Session, pipeline_ids: list[uuid.UUID]) -> dict[uuid.UUID, Update]:
    if not pipeline_ids:
        return {}
    rows = session.execute(
        select(
            PipelineRun.pipeline_id,
            func.max(PipelineRun.finished_at),
            func.max(PipelineRun.business_date),
        )
        .where(
            PipelineRun.pipeline_id.in_(pipeline_ids),
            PipelineRun.status == RunStatus.SUCCEEDED.value,
        )
        .group_by(PipelineRun.pipeline_id)
    )
    return {pipeline_id: (finished_at, day) for pipeline_id, finished_at, day in rows}


def evaluate(
    session: Session, pairs: list[tuple[Dataset, FreshnessRule]], now: datetime
) -> list[FreshnessOut]:
    """Evaluate rules with two grouped queries, whatever the number of datasets."""
    landed = _landed_updates(
        session,
        [d.id for d, r in pairs if r.signal == FreshnessSignal.LANDED_FILE.value],
    )
    runs = _run_updates(
        session,
        list({r.pipeline_id for _, r in pairs if r.pipeline_id is not None}),
    )
    results = []
    for dataset, rule in pairs:
        if rule.signal == FreshnessSignal.LANDED_FILE.value:
            last_at, last_day = landed.get(dataset.id, (None, None))
        else:
            last_at, last_day = runs.get(rule.pipeline_id, (None, None))
        status, age = classify(
            Thresholds(rule.warn_after_minutes, rule.error_after_minutes), last_at, now
        )
        results.append(
            FreshnessOut(
                dataset_id=dataset.id,
                dataset_name=dataset.name,
                layer=Layer(dataset.layer),
                status=status,
                signal=FreshnessSignal(rule.signal),
                pipeline_id=rule.pipeline_id,
                warn_after_minutes=rule.warn_after_minutes,
                error_after_minutes=rule.error_after_minutes,
                last_updated_at=as_utc(last_at) if last_at is not None else None,
                last_business_date=last_day,
                age_seconds=age,
                evaluated_at=now,
            )
        )
    return results


@dataset_router.get("/freshness-rule", response_model=RuleOut)
def get_rule(
    dataset_id: uuid.UUID,
    access: WorkspaceAccess = Depends(require_role(Role.VIEWER)),
    session: Session = Depends(get_session),
) -> RuleOut:
    dataset = load_dataset(session, access.workspace.id, dataset_id)
    rule = _load_rule(session, dataset)
    if rule is None:
        raise HTTPException(404, "This dataset has no freshness rule")
    return _rule_out(rule)


@dataset_router.put("/freshness-rule", response_model=RuleOut)
def put_rule(
    dataset_id: uuid.UUID,
    body: RuleWrite,
    response: Response,
    access: WorkspaceAccess = Depends(require_role(Role.ENGINEER)),
    session: Session = Depends(get_session),
) -> RuleOut:
    workspace_id = access.workspace.id
    # Lock the dataset row so two first PUTs cannot both pass the checks below.
    dataset = load_dataset(session, workspace_id, dataset_id, lock=True)

    error_after = body.error_after_minutes or dataset.freshness_sla_minutes
    if error_after is None:
        raise HTTPException(
            422, "error_after_minutes is required when the dataset has no freshness SLA"
        )
    if body.warn_after_minutes is not None and body.warn_after_minutes >= error_after:
        raise HTTPException(422, "warn_after_minutes must be below error_after_minutes")
    if body.signal is FreshnessSignal.LANDED_FILE and dataset.layer != Layer.RAW.value:
        raise HTTPException(422, "Only raw datasets receive landed files; use pipeline_run")
    if body.pipeline_id is not None:
        pipeline = session.scalar(
            select(Pipeline.id).where(
                Pipeline.id == body.pipeline_id, Pipeline.workspace_id == workspace_id
            )
        )
        if pipeline is None:
            raise HTTPException(422, "The pipeline must belong to this workspace")

    rule = _load_rule(session, dataset, lock=True)
    if rule is None:
        if body.version is not None:
            raise HTTPException(409, "This dataset has no freshness rule to replace")
        rule = FreshnessRule(dataset_id=dataset.id, version=1)
        session.add(rule)
        response.status_code = 201
    else:
        if body.version is None:
            raise HTTPException(
                409, f"This dataset already has a freshness rule (version {rule.version})"
            )
        if body.version != rule.version:
            raise HTTPException(
                409, f"Rule was changed by someone else (current version {rule.version})"
            )
        rule.version += 1

    rule.signal = body.signal.value
    rule.pipeline_id = body.pipeline_id
    rule.warn_after_minutes = body.warn_after_minutes
    rule.error_after_minutes = error_after
    rule.updated_by_user_id = access.user.id
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        raise HTTPException(409, "The freshness rule was changed concurrently") from None
    session.refresh(rule)
    logger.info(
        "freshness rule saved dataset=%s version=%s signal=%s", dataset.id, rule.version, rule.signal
    )
    return _rule_out(rule)


@dataset_router.delete("/freshness-rule", status_code=204)
def delete_rule(
    dataset_id: uuid.UUID,
    access: WorkspaceAccess = Depends(require_role(Role.ENGINEER)),
    session: Session = Depends(get_session),
) -> Response:
    dataset = load_dataset(session, access.workspace.id, dataset_id)
    rule = _load_rule(session, dataset, lock=True)
    if rule is None:
        raise HTTPException(404, "This dataset has no freshness rule")
    session.delete(rule)
    session.commit()
    logger.info("freshness rule deleted dataset=%s", dataset.id)
    return Response(status_code=204)


@dataset_router.get("/freshness", response_model=FreshnessOut)
def dataset_freshness(
    dataset_id: uuid.UUID,
    access: WorkspaceAccess = Depends(require_role(Role.VIEWER)),
    session: Session = Depends(get_session),
    now: datetime = Depends(utc_now),
) -> FreshnessOut:
    dataset = load_dataset(session, access.workspace.id, dataset_id)
    rule = _load_rule(session, dataset)
    if rule is None:
        raise HTTPException(404, "This dataset has no freshness rule")
    return evaluate(session, [(dataset, rule)], now)[0]


@workspace_router.get("/freshness", response_model=FreshnessList)
def workspace_freshness(
    access: WorkspaceAccess = Depends(require_role(Role.VIEWER)),
    session: Session = Depends(get_session),
    now: datetime = Depends(utc_now),
    status: list[FreshnessStatus] | None = Query(default=None),
) -> FreshnessList:
    """Every dataset with a rule, worst status first, then by name.

    `counts` covers all rules; `status` (repeatable) only filters `items`.
    """
    pairs = session.execute(
        select(Dataset, FreshnessRule)
        .join(FreshnessRule, FreshnessRule.dataset_id == Dataset.id)
        .where(Dataset.workspace_id == access.workspace.id)
    ).tuples().all()
    results = evaluate(session, list(pairs), now)
    counts = {s: 0 for s in FreshnessStatus}
    for result in results:
        counts[result.status] += 1
    if status:
        results = [r for r in results if r.status in status]
    results.sort(key=lambda r: (r.status.severity, r.dataset_name))
    return FreshnessList(evaluated_at=now, counts=counts, items=results)
