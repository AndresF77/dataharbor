"""Daily raw-source ingestion with run tracking in DataHarbor.

Each run covers one business date (Airflow's `ds`: the run at 03:00 UTC on
day D+1 lands day D, matching the fixtures' 03:00 `expected_by`):

    open_run -> land_raw_customers, land_raw_orders, land_raw_payments -> close_run

`open_run` registers the run in DataHarbor under Airflow's `run_id`, so a
retried or cleared task reuses the same DataHarbor run. A land task fails
without retrying when the file breaks its contract (the outcome is already
recorded); API or network errors are retried. `close_run` always runs,
finishes the DataHarbor run, and fails the DAG run if any task failed.

Settings come from DATAHARBOR_* environment variables (services/worker/README.md);
the task logic lives in `dataharbor_worker.ingestion`.
"""

from datetime import date, timedelta

import pendulum
from airflow.decorators import dag, task
from airflow.exceptions import AirflowFailException

from dataharbor_worker.config import WorkerConfig
from dataharbor_worker.ingestion import (
    PIPELINE_NAME,
    RAW_DATASETS,
    RunContext,
    close_run,
    land_dataset,
    open_run,
    task_id_for,
    trigger_for,
)

DAG_ID = "dataharbor_ingest_raw_sources"
SCHEDULE = "0 3 * * *"


def _context(run: dict) -> tuple[WorkerConfig, RunContext]:
    config = WorkerConfig.from_env()
    ctx = RunContext(config.client(), run["pipeline_id"], run["run_id"],
                     date.fromisoformat(run["business_date"]))
    return config, ctx


@dag(
    dag_id=DAG_ID,
    description=f"Land raw source files and track the run ({PIPELINE_NAME})",
    schedule=SCHEDULE,
    start_date=pendulum.datetime(2026, 2, 23, tz="UTC"),
    catchup=False,
    max_active_runs=1,
    default_args={"retries": 2, "retry_delay": timedelta(minutes=1)},
    tags=["dataharbor", "ingestion"],
)
def ingest_raw_sources():
    @task
    def start(ds=None, run_id=None, dag_run=None) -> dict:
        config = WorkerConfig.from_env()
        ctx = open_run(config.client(), run_id, date.fromisoformat(ds),
                       trigger_for(str(dag_run.run_type)))
        return {"pipeline_id": ctx.pipeline_id, "run_id": ctx.run_id, "business_date": ds}

    @task
    def land(dataset_name: str, run: dict) -> str:
        config, ctx = _context(run)
        outcome = land_dataset(ctx, config.source_dir, dataset_name)
        if outcome.status == "failed":
            raise AirflowFailException(outcome.message)
        return outcome.status

    @task(trigger_rule="all_done")
    def finish(run: dict) -> str:
        _, ctx = _context(run)
        result = close_run(ctx, [task_id_for(name) for name in RAW_DATASETS])
        if result["status"] != "succeeded":
            raise AirflowFailException(result.get("error_summary") or "Run failed")
        return result["status"]

    run = start.override(task_id="open_run")()
    landed = [land.override(task_id=task_id_for(name))(name, run) for name in RAW_DATASETS]
    landed >> finish.override(task_id="close_run")(run)


ingest_raw_sources()
