"""Run raw-source ingestion for one business date without Airflow.

    python -m dataharbor_worker ingest --business-date 2026-03-01

Reads the DATAHARBOR_* variables listed in services/worker/README.md and
prints the finished run as JSON. Exit code 1 when the run failed.
"""

import argparse
import json
import logging
import sys
from datetime import date, datetime, timezone

from dataharbor_worker.config import ConfigError, WorkerConfig
from dataharbor_worker.ingestion import SourceError, ingest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m dataharbor_worker")
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("ingest", help="land one business date's raw source files")
    run.add_argument("--business-date", type=date.fromisoformat, required=True)
    run.add_argument("--run-id", help="external run id; defaults to manual__<UTC timestamp>")
    run.add_argument("--trigger", choices=["manual", "backfill"], default="manual")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    try:
        config = WorkerConfig.from_env()
    except ConfigError as exc:
        parser.error(str(exc))
    run_id = args.run_id or f"manual__{datetime.now(timezone.utc).isoformat(timespec='seconds')}"
    try:
        summary = ingest(config.client(), config.source_dir, args.business_date, run_id,
                         args.trigger)
    except SourceError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(summary.run, indent=2))
    return 0 if summary.status == "succeeded" else 1


if __name__ == "__main__":
    sys.exit(main())
