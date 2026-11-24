"""Pipelines and run tracking: `/v1/workspaces/{workspace_id}/pipelines`.

The orchestrator (the Airflow DAGs in `services/worker`) reports each run
with an engineer token:

1. `POST …/{pipeline_id}/runs` opens a run for a business date. The
   orchestrator's run id is unique per pipeline, so a retried start returns
   the same DataHarbor run (`200`) instead of a second one.
2. `POST …/runs/{run_id}/tasks` records one task outcome. Outcomes are
   written once; repeating the same report is a no-op, a different one `409`.
3. `POST …/runs/{run_id}/finish` closes the run as `succeeded` or `failed`.
   A run with a failed task cannot succeed, and a finished run is never
   changed again.

Viewers can read pipelines and runs. `GET …/{pipeline_id}/runs` is the run
history: newest business date first, filterable by status, trigger and a
business-date window, with task counts per run and keyset pagination. Task messages and error summaries come
from the orchestrator, are bounded to 500 characters and are stored as text.
"""

import base64
import binascii
import logging
import uuid
from datetime import date, datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import and_, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from dataharbor_api.auth import WorkspaceAccess, get_session, require_role
from dataharbor_api.datasets import load_dataset
from dataharbor_api.models import (
    LandedFile,
    Pipeline,
    PipelineKind,
    PipelineRun,
    PipelineRunTask,
    Role,
    RunStatus,
    RunTrigger,
    TaskStatus,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/workspaces/{workspace_id}/pipelines", tags=["pipelines"])

EXTERNAL_ID_PATTERN = r"^[\x21-\x7e]([\x20-\x7e]{0,248}[\x21-\x7e])?$"  # printable ASCII
TASK_ID_PATTERN = r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,249}$"


class PipelineOut(BaseModel):
    id: uuid.UUID
    name: str
    kind: PipelineKind
    dag_id: str
    schedule: str | None
    description: str
    created_at: datetime
    updated_at: datetime


class PipelineList(BaseModel):
    items: list[PipelineOut]


class RunStart(BaseModel):
    model_config = ConfigDict(extra="forbid")

    external_run_id: str = Field(
        pattern=EXTERNAL_ID_PATTERN, description="The orchestrator's run id (Airflow `run_id`)"
    )
    business_date: date
    trigger: RunTrigger


class TaskReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task_id: str = Field(pattern=TASK_ID_PATTERN)
    status: TaskStatus
    dataset_id: uuid.UUID | None = None
    landed_file_id: uuid.UUID | None = Field(
        default=None, description="Needs `dataset_id`; must be a file of that dataset"
    )
    message: str = Field(default="", max_length=500)


class RunFinish(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: RunStatus = Field(description="`succeeded` or `failed`")
    error_summary: str | None = Field(default=None, min_length=1, max_length=500)


class TaskOut(BaseModel):
    task_id: str
    status: TaskStatus
    dataset_id: uuid.UUID | None
    landed_file_id: uuid.UUID | None
    message: str
    recorded_at: datetime


class RunOut(BaseModel):
    id: uuid.UUID
    pipeline_id: uuid.UUID
    external_run_id: str
    trigger: RunTrigger
    business_date: date
    status: RunStatus
    started_by_user_id: uuid.UUID | None
    error_summary: str | None
    started_at: datetime
    finished_at: datetime | None
    tasks: list[TaskOut]


class TaskCounts(BaseModel):
    succeeded: int = 0
    failed: int = 0
    skipped: int = 0


class RunSummary(BaseModel):
    id: uuid.UUID
    external_run_id: str
    trigger: RunTrigger
    business_date: date
    status: RunStatus
    error_summary: str | None
    started_at: datetime
    finished_at: datetime | None
    duration_seconds: float | None = Field(description="Null while the run is still running")
    task_counts: TaskCounts


class RunPage(BaseModel):
    items: list[RunSummary]
    next_cursor: str | None


def encode_run_cursor(run: PipelineRun) -> str:
    raw = f"{run.business_date.isoformat()}|{run.id}"
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


def decode_run_cursor(cursor: str) -> tuple[date, uuid.UUID]:
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)).decode()
        day, run_id = raw.split("|")
        return date.fromisoformat(day), uuid.UUID(run_id)
    except (binascii.Error, UnicodeDecodeError, ValueError):
        raise HTTPException(422, "Invalid cursor") from None


def _duration(run: PipelineRun) -> float | None:
    if run.finished_at is None:
        return None
    started, finished = run.started_at, run.finished_at
    if (started.tzinfo is None) != (finished.tzinfo is None):
        started, finished = started.replace(tzinfo=None), finished.replace(tzinfo=None)
    return max((finished - started).total_seconds(), 0.0)


def _task_counts(session: Session, run_ids: list[uuid.UUID]) -> dict[uuid.UUID, TaskCounts]:
    counts = {run_id: TaskCounts() for run_id in run_ids}
    if not run_ids:
        return counts
    rows = session.execute(
        select(PipelineRunTask.run_id, PipelineRunTask.status, func.count())
        .where(PipelineRunTask.run_id.in_(run_ids))
        .group_by(PipelineRunTask.run_id, PipelineRunTask.status)
    ).all()
    for run_id, status, count in rows:
        setattr(counts[run_id], TaskStatus(status).value, count)
    return counts


def _pipeline_out(row: Pipeline) -> PipelineOut:
    return PipelineOut(
        id=row.id,
        name=row.name,
        kind=PipelineKind(row.kind),
        dag_id=row.dag_id,
        schedule=row.schedule,
        description=row.description,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _task_out(row: PipelineRunTask) -> TaskOut:
    return TaskOut(
        task_id=row.task_id,
        status=TaskStatus(row.status),
        dataset_id=row.dataset_id,
        landed_file_id=row.landed_file_id,
        message=row.message,
        recorded_at=row.recorded_at,
    )


def _run_out(session: Session, run: PipelineRun) -> RunOut:
    tasks = session.scalars(
        select(PipelineRunTask)
        .where(PipelineRunTask.run_id == run.id)
        .order_by(PipelineRunTask.recorded_at, PipelineRunTask.task_id)
    ).all()
    return RunOut(
        id=run.id,
        pipeline_id=run.pipeline_id,
        external_run_id=run.external_run_id,
        trigger=RunTrigger(run.trigger),
        business_date=run.business_date,
        status=RunStatus(run.status),
        started_by_user_id=run.started_by_user_id,
        error_summary=run.error_summary,
        started_at=run.started_at,
        finished_at=run.finished_at,
        tasks=[_task_out(t) for t in tasks],
    )


def load_pipeline(session: Session, workspace_id: uuid.UUID, pipeline_id: uuid.UUID) -> Pipeline:
    pipeline = session.scalar(
        select(Pipeline).where(Pipeline.id == pipeline_id, Pipeline.workspace_id == workspace_id)
    )
    if pipeline is None:
        raise HTTPException(404, "Pipeline not found")
    return pipeline


def load_run(
    session: Session, pipeline: Pipeline, run_id: uuid.UUID, *, lock: bool = False
) -> PipelineRun:
    query = select(PipelineRun).where(
        PipelineRun.id == run_id, PipelineRun.pipeline_id == pipeline.id
    )
    if lock:
        query = query.with_for_update()
    run = session.scalar(query)
    if run is None:
        raise HTTPException(404, "Run not found")
    return run


def _by_external_id(session: Session, pipeline: Pipeline, external_run_id: str) -> PipelineRun | None:
    return session.scalar(
        select(PipelineRun).where(
            PipelineRun.pipeline_id == pipeline.id,
            PipelineRun.external_run_id == external_run_id,
        )
    )


def _same_start(run: PipelineRun, body: RunStart) -> bool:
    return run.business_date == body.business_date and run.trigger == body.trigger.value


@router.get("", response_model=PipelineList)
def list_pipelines(
    access: WorkspaceAccess = Depends(require_role(Role.VIEWER)),
    session: Session = Depends(get_session),
) -> PipelineList:
    rows = session.scalars(
        select(Pipeline).where(Pipeline.workspace_id == access.workspace.id).order_by(Pipeline.name)
    ).all()
    return PipelineList(items=[_pipeline_out(r) for r in rows])


@router.get("/{pipeline_id}", response_model=PipelineOut)
def get_pipeline(
    pipeline_id: uuid.UUID,
    access: WorkspaceAccess = Depends(require_role(Role.VIEWER)),
    session: Session = Depends(get_session),
) -> PipelineOut:
    return _pipeline_out(load_pipeline(session, access.workspace.id, pipeline_id))


@router.post("/{pipeline_id}/runs", response_model=RunOut, status_code=201)
def start_run(
    pipeline_id: uuid.UUID,
    body: RunStart,
    response: Response,
    access: WorkspaceAccess = Depends(require_role(Role.ENGINEER)),
    session: Session = Depends(get_session),
) -> RunOut:
    """Open a run, or return the one already opened for this `external_run_id`."""
    pipeline = load_pipeline(session, access.workspace.id, pipeline_id)

    def replay(run: PipelineRun) -> RunOut:
        if not _same_start(run, body):
            raise HTTPException(
                409, "This external_run_id was already started with a different date or trigger"
            )
        response.status_code = 200
        return _run_out(session, run)

    existing = _by_external_id(session, pipeline, body.external_run_id)
    if existing is not None:
        return replay(existing)

    run = PipelineRun(
        pipeline_id=pipeline.id,
        external_run_id=body.external_run_id,
        trigger=body.trigger.value,
        business_date=body.business_date,
        status=RunStatus.RUNNING.value,
        started_by_user_id=access.user.id,
    )
    session.add(run)
    try:
        session.commit()
    except IntegrityError:
        # The same start arrived twice at once; the first insert wins.
        session.rollback()
        existing = _by_external_id(session, pipeline, body.external_run_id)
        if existing is None:
            raise
        return replay(existing)
    session.refresh(run)
    logger.info(
        "run started pipeline=%s run=%s business_date=%s trigger=%s",
        pipeline.id, run.id, run.business_date, run.trigger,
    )
    return _run_out(session, run)


@router.get("/{pipeline_id}/runs", response_model=RunPage)
def list_runs(
    pipeline_id: uuid.UUID,
    status: RunStatus | None = None,
    trigger: RunTrigger | None = None,
    business_date_from: date | None = Query(default=None, description="Inclusive"),
    business_date_to: date | None = Query(default=None, description="Inclusive"),
    limit: int = Query(default=50, ge=1, le=200),
    cursor: str | None = Query(default=None, max_length=128),
    access: WorkspaceAccess = Depends(require_role(Role.VIEWER)),
    session: Session = Depends(get_session),
) -> RunPage:
    """Run history, newest business date first; pass `next_cursor` back for more.

    Runs of the same business date (retries, backfills) come in a stable
    order, so pages never skip or repeat a run.
    """
    if business_date_from and business_date_to and business_date_from > business_date_to:
        raise HTTPException(422, "business_date_from is after business_date_to")
    pipeline = load_pipeline(session, access.workspace.id, pipeline_id)

    query = select(PipelineRun).where(PipelineRun.pipeline_id == pipeline.id)
    if status is not None:
        query = query.where(PipelineRun.status == status.value)
    if trigger is not None:
        query = query.where(PipelineRun.trigger == trigger.value)
    if business_date_from is not None:
        query = query.where(PipelineRun.business_date >= business_date_from)
    if business_date_to is not None:
        query = query.where(PipelineRun.business_date <= business_date_to)
    if cursor is not None:
        after_date, after_id = decode_run_cursor(cursor)
        query = query.where(
            or_(
                PipelineRun.business_date < after_date,
                and_(PipelineRun.business_date == after_date, PipelineRun.id < after_id),
            )
        )
    rows = session.scalars(
        query.order_by(PipelineRun.business_date.desc(), PipelineRun.id.desc()).limit(limit + 1)
    ).all()
    runs = rows[:limit]
    counts = _task_counts(session, [r.id for r in runs])
    items = [
        RunSummary(
            id=r.id,
            external_run_id=r.external_run_id,
            trigger=RunTrigger(r.trigger),
            business_date=r.business_date,
            status=RunStatus(r.status),
            error_summary=r.error_summary,
            started_at=r.started_at,
            finished_at=r.finished_at,
            duration_seconds=_duration(r),
            task_counts=counts[r.id],
        )
        for r in runs
    ]
    next_cursor = encode_run_cursor(runs[-1]) if len(rows) > limit else None
    return RunPage(items=items, next_cursor=next_cursor)


@router.get("/{pipeline_id}/runs/{run_id}", response_model=RunOut)
def get_run(
    pipeline_id: uuid.UUID,
    run_id: uuid.UUID,
    access: WorkspaceAccess = Depends(require_role(Role.VIEWER)),
    session: Session = Depends(get_session),
) -> RunOut:
    pipeline = load_pipeline(session, access.workspace.id, pipeline_id)
    return _run_out(session, load_run(session, pipeline, run_id))


def _same_task(row: PipelineRunTask, body: TaskReport) -> bool:
    return (
        row.status == body.status.value
        and row.dataset_id == body.dataset_id
        and row.landed_file_id == body.landed_file_id
        and row.message == body.message
    )


@router.post("/{pipeline_id}/runs/{run_id}/tasks", response_model=RunOut, status_code=201)
def record_task(
    pipeline_id: uuid.UUID,
    run_id: uuid.UUID,
    body: TaskReport,
    response: Response,
    access: WorkspaceAccess = Depends(require_role(Role.ENGINEER)),
    session: Session = Depends(get_session),
) -> RunOut:
    pipeline = load_pipeline(session, access.workspace.id, pipeline_id)
    run = load_run(session, pipeline, run_id, lock=True)

    existing = session.scalar(
        select(PipelineRunTask).where(
            PipelineRunTask.run_id == run.id, PipelineRunTask.task_id == body.task_id
        )
    )
    if existing is not None:
        if not _same_task(existing, body):
            raise HTTPException(409, f"Task {body.task_id} already has a different outcome")
        response.status_code = 200
        return _run_out(session, run)
    if run.status != RunStatus.RUNNING.value:
        raise HTTPException(409, "The run is finished; its history cannot change")

    if body.landed_file_id is not None and body.dataset_id is None:
        raise HTTPException(422, "landed_file_id needs dataset_id")
    if body.dataset_id is not None:
        try:
            dataset = load_dataset(session, access.workspace.id, body.dataset_id)
        except HTTPException:
            raise HTTPException(422, "dataset_id is not a dataset of this workspace") from None
        if body.landed_file_id is not None:
            landed = session.scalar(
                select(LandedFile.id).where(
                    LandedFile.id == body.landed_file_id, LandedFile.dataset_id == dataset.id
                )
            )
            if landed is None:
                raise HTTPException(422, "landed_file_id is not a file of that dataset")

    session.add(
        PipelineRunTask(
            run_id=run.id,
            task_id=body.task_id,
            status=body.status.value,
            dataset_id=body.dataset_id,
            landed_file_id=body.landed_file_id,
            message=body.message,
        )
    )
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        raise HTTPException(409, f"Task {body.task_id} was recorded concurrently") from None
    logger.info("run task run=%s task=%s status=%s", run.id, body.task_id, body.status.value)
    return _run_out(session, run)


@router.post("/{pipeline_id}/runs/{run_id}/finish", response_model=RunOut)
def finish_run(
    pipeline_id: uuid.UUID,
    run_id: uuid.UUID,
    body: RunFinish,
    access: WorkspaceAccess = Depends(require_role(Role.ENGINEER)),
    session: Session = Depends(get_session),
) -> RunOut:
    if body.status is RunStatus.RUNNING:
        raise HTTPException(422, "A run can only finish as succeeded or failed")
    if body.status is RunStatus.SUCCEEDED and body.error_summary is not None:
        raise HTTPException(422, "error_summary is only for failed runs")

    pipeline = load_pipeline(session, access.workspace.id, pipeline_id)
    run = load_run(session, pipeline, run_id, lock=True)

    if run.status != RunStatus.RUNNING.value:
        if run.status == body.status.value and run.error_summary == body.error_summary:
            return _run_out(session, run)
        raise HTTPException(409, f"The run already finished as {run.status}")

    if body.status is RunStatus.SUCCEEDED:
        failed = session.scalar(
            select(func.count())
            .select_from(PipelineRunTask)
            .where(
                PipelineRunTask.run_id == run.id,
                PipelineRunTask.status == TaskStatus.FAILED.value,
            )
        )
        if failed:
            raise HTTPException(409, f"The run has {failed} failed task(s) and cannot succeed")

    run.status = body.status.value
    run.error_summary = body.error_summary
    run.finished_at = datetime.now(timezone.utc)
    session.commit()
    session.refresh(run)
    logger.info("run finished pipeline=%s run=%s status=%s", pipeline.id, run.id, run.status)
    return _run_out(session, run)
