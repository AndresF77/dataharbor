"""Parses the DAG file when Airflow is installed (it is not needed for the other tests)."""

from pathlib import Path

import pytest

DAGS = Path(__file__).resolve().parents[1] / "dags"


def test_ingest_dag_loads():
    models = pytest.importorskip("airflow.models")

    bag = models.DagBag(dag_folder=str(DAGS), include_examples=False)

    assert bag.import_errors == {}
    dag = bag.get_dag("dataharbor_ingest_raw_sources")
    assert set(dag.task_ids) == {
        "open_run", "land_raw_customers", "land_raw_orders", "land_raw_payments", "close_run",
    }
    assert dag.get_task("close_run").trigger_rule == "all_done"
