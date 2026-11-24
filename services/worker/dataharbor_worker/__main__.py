"""Worker tasks for one business date, without Airflow.

    python -m dataharbor_worker ingest --business-date 2026-03-01
    python -m dataharbor_worker load-raw --business-date 2026-03-01

`ingest` lands the source files and prints the finished run as JSON (exit
code 1 when the run failed). `load-raw` copies the files that landed for
that date into the warehouse `raw` tables for dbt and prints one line per
dataset (exit code 1 when a dataset failed). Settings come from the
DATAHARBOR_* variables listed in services/worker/README.md.
"""

import argparse
import json
import logging
import sys
from datetime import date, datetime, timezone

from dataharbor_worker.config import ConfigError, WorkerConfig
from dataharbor_worker.ingestion import SourceError, ingest
from dataharbor_worker.warehouse import LoadError, connect, load_business_date


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m dataharbor_worker")
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("ingest", help="land one business date's raw source files")
    run.add_argument("--business-date", type=date.fromisoformat, required=True)
    run.add_argument("--run-id", help="external run id; defaults to manual__<UTC timestamp>")
    run.add_argument("--trigger", choices=["manual", "backfill"], default="manual")
    load = commands.add_parser("load-raw",
                               help="load one business date's landed files into raw.* tables")
    load.add_argument("--business-date", type=date.fromisoformat, required=True)
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    try:
        config = WorkerConfig.from_env()
    except ConfigError as exc:
        parser.error(str(exc))
    if args.command == "load-raw":
        return _load_raw(parser, config, args.business_date)
    run_id = args.run_id or f"manual__{datetime.now(timezone.utc).isoformat(timespec='seconds')}"
    try:
        summary = ingest(config.client(), config.source_dir, args.business_date, run_id,
                         args.trigger)
    except SourceError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(summary.run, indent=2))
    return 0 if summary.status == "succeeded" else 1


def _load_raw(parser: argparse.ArgumentParser, config: WorkerConfig, business_date: date) -> int:
    try:
        conn = connect()
    except LoadError as exc:
        parser.error(str(exc))
    try:
        outcomes = load_business_date(config.client(), conn, config.source_dir, business_date)
    except SourceError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    finally:
        conn.close()
    for outcome in outcomes:
        print(f"{outcome.dataset}: {outcome.status} {outcome.message}".rstrip())
    return 1 if any(o.status == "failed" for o in outcomes) else 0


if __name__ == "__main__":
    sys.exit(main())
