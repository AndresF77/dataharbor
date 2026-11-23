"""Raw-source ingestion: land one business date's files and track the run.

The source directory has the layout written by `packages/fixtures`
(`manifest.json` plus `customers/`, `orders/`, `payments/`). For each raw
dataset the run:

1. picks the manifest entry for that source and business date — none means
   the task is `skipped` (the customers snapshot only exists for the last date);
2. lands the bytes through the API, sending the manifest SHA-256 as
   `X-Content-SHA256` so a file changed after delivery is refused;
3. records the task outcome on the run.

Every step can be retried: the run is keyed by the orchestrator's run id,
landing the same bytes again is recognised by the API, and task messages
are built only from the file's own facts, so a retry reports the same
outcome. The run fails if any task failed. Messages carry counts, rule
names and checksums — never file contents.
"""

import json
import logging
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path, PurePosixPath

from dataharbor_worker.api_client import ApiClient, ApiError

logger = logging.getLogger(__name__)

PIPELINE_NAME = "ingest_raw_sources"
RAW_DATASETS = ("raw_customers", "raw_orders", "raw_payments")
MAX_MESSAGE = 500

_TRIGGERS = {"scheduled": "scheduled", "manual": "manual", "backfill": "backfill",
             "dataset_triggered": "scheduled"}


class SourceError(Exception):
    """The source directory or its manifest cannot be used."""


def trigger_for(run_type: str) -> str:
    """Map an Airflow `DagRunType` value to a DataHarbor run trigger."""
    return _TRIGGERS.get(run_type, "manual")


def task_id_for(dataset_name: str) -> str:
    return f"land_{dataset_name}"


@dataclass(frozen=True)
class ManifestEntry:
    path: str
    source: str
    business_date: date
    sha256: str
    record_count: int


def load_manifest(source_dir: Path) -> list[ManifestEntry]:
    path = Path(source_dir) / "manifest.json"
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
        return [
            ManifestEntry(
                path=entry["path"],
                source=entry["source"],
                business_date=date.fromisoformat(entry["business_date"]),
                sha256=entry["sha256"],
                record_count=int(entry["record_count"]),
            )
            for entry in manifest["files"]
        ]
    except FileNotFoundError:
        raise SourceError(f"No manifest.json in {source_dir}") from None
    except (ValueError, KeyError, TypeError) as exc:
        raise SourceError(f"Unreadable manifest.json: {type(exc).__name__}") from None


def entry_for(entries: Iterable[ManifestEntry], dataset_name: str,
              business_date: date) -> ManifestEntry | None:
    source = dataset_name.removeprefix("raw_")
    matches = [e for e in entries if e.source == source and e.business_date == business_date]
    if len(matches) > 1:
        raise SourceError(f"Manifest lists {len(matches)} {source} files for {business_date}")
    return matches[0] if matches else None


def _read_source(source_dir: Path, relative: str) -> bytes | None:
    """Read a manifest path, refusing anything that would leave `source_dir`."""
    rel = PurePosixPath(relative)
    if rel.is_absolute() or ".." in rel.parts:
        raise SourceError(f"Manifest path escapes the source directory: {relative!r}")
    try:
        return (Path(source_dir) / rel).read_bytes()
    except FileNotFoundError:
        return None


def _bounded(text: str) -> str:
    return text if len(text) <= MAX_MESSAGE else text[: MAX_MESSAGE - 1] + "…"


@dataclass
class TaskOutcome:
    task_id: str
    status: str
    dataset_id: str | None = None
    landed_file_id: str | None = None
    message: str = ""


@dataclass
class RunContext:
    """What every task of one run needs; cheap to rebuild in each Airflow task."""

    client: ApiClient
    pipeline_id: str
    run_id: str
    business_date: date


@dataclass
class RunSummary:
    run: dict
    outcomes: list[dict] = field(default_factory=list)

    @property
    def status(self) -> str:
        return self.run["status"]


def open_run(client: ApiClient, external_run_id: str, business_date: date,
             trigger: str) -> RunContext:
    pipeline = client.pipeline_by_name(PIPELINE_NAME)
    run = client.start_run(pipeline["id"], external_run_id, business_date, trigger)
    logger.info("run open pipeline=%s run=%s business_date=%s", pipeline["id"], run["id"],
                business_date)
    return RunContext(client, pipeline["id"], run["id"], business_date)


def land_dataset(ctx: RunContext, source_dir: Path, dataset_name: str,
                 datasets: dict[str, dict] | None = None) -> TaskOutcome:
    """Land one raw dataset's file for the run's business date and record the outcome."""
    task_id = task_id_for(dataset_name)
    datasets = datasets if datasets is not None else ctx.client.raw_datasets()
    dataset = datasets.get(dataset_name)
    outcome = _land(ctx, Path(source_dir), task_id, dataset_name, dataset)
    ctx.client.record_task(
        ctx.pipeline_id,
        ctx.run_id,
        task_id=outcome.task_id,
        status=outcome.status,
        dataset_id=outcome.dataset_id,
        landed_file_id=outcome.landed_file_id,
        message=_bounded(outcome.message),
    )
    logger.info("task %s run=%s status=%s", task_id, ctx.run_id, outcome.status)
    return outcome


def _land(ctx: RunContext, source_dir: Path, task_id: str, dataset_name: str,
          dataset: dict | None) -> TaskOutcome:
    if dataset is None:
        return TaskOutcome(task_id, "failed", message=f"Dataset {dataset_name} is not registered")
    failed = TaskOutcome(task_id, "failed", dataset_id=dataset["id"])

    try:
        entry = entry_for(load_manifest(source_dir), dataset_name, ctx.business_date)
    except SourceError as exc:
        failed.message = str(exc)
        return failed
    if entry is None:
        return TaskOutcome(task_id, "skipped", dataset_id=dataset["id"],
                           message=f"No {dataset_name} file for {ctx.business_date}")

    filename = PurePosixPath(entry.path).name
    try:
        data = _read_source(source_dir, entry.path)
    except SourceError as exc:
        failed.message = str(exc)
        return failed
    if data is None:
        failed.message = f"{filename} is listed in the manifest but missing"
        return failed

    try:
        landed = ctx.client.land_file(dataset["id"], data, ctx.business_date, filename,
                                      entry.sha256)
    except ApiError as exc:
        if exc.status >= 500 or exc.status in (401, 403, 404):
            raise  # infrastructure or access problem: let the orchestrator retry
        failed.message = _rejection(filename, exc)
        return failed
    return TaskOutcome(
        task_id,
        "succeeded",
        dataset_id=dataset["id"],
        landed_file_id=landed["id"],
        message=(
            f"{filename}: {landed['record_count']} records, contract v"
            f"{landed['contract_version']}, sha256 {landed['sha256'][:12]}"
        ),
    )


def _rejection(filename: str, exc: ApiError) -> str:
    details = exc.details if isinstance(exc.details, dict) else {}
    if "violation_count" in details:
        rules = sorted({v["rule"] for v in details.get("violations", [])})
        return (
            f"{filename} rejected by contract v{details.get('contract_version')}: "
            f"{details['violation_count']} violation(s) ({', '.join(rules)})"
        )
    return f"{filename} rejected: {exc.message}"


def close_run(ctx: RunContext, expected_tasks: Iterable[str]) -> dict:
    """Finish the run from its recorded tasks; a task that never reported counts as failed."""
    run = ctx.client.get_run(ctx.pipeline_id, ctx.run_id)
    if run["status"] != "running":
        return run
    recorded = {t["task_id"]: t["status"] for t in run["tasks"]}
    failed = [t for t in recorded if recorded[t] == "failed"]
    missing = [t for t in expected_tasks if t not in recorded]
    if not failed and not missing:
        return ctx.client.finish_run(ctx.pipeline_id, ctx.run_id, "succeeded")
    parts = []
    if failed:
        parts.append(f"{len(failed)} task(s) failed: {', '.join(sorted(failed))}")
    if missing:
        parts.append(f"{len(missing)} task(s) did not report: {', '.join(missing)}")
    return ctx.client.finish_run(ctx.pipeline_id, ctx.run_id, "failed",
                                 _bounded("; ".join(parts)))


def ingest(client: ApiClient, source_dir: Path, business_date: date, external_run_id: str,
           trigger: str = "manual") -> RunSummary:
    """Run the whole pipeline in one process (CLI and tests); same steps as the DAG."""
    ctx = open_run(client, external_run_id, business_date, trigger)
    datasets = client.raw_datasets()
    outcomes = [land_dataset(ctx, source_dir, name, datasets).__dict__ for name in RAW_DATASETS]
    run = close_run(ctx, [task_id_for(n) for n in RAW_DATASETS])
    return RunSummary(run=run, outcomes=outcomes)
