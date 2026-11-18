"""Generate or check DataHarbor source fixtures.

    python3 -m dataharbor_fixtures generate --out DIR [--seed N] [--defects]
    python3 -m dataharbor_fixtures check DIR
"""

from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

from .generator import DEFAULT_DAYS, DEFAULT_REFERENCE_DATE, DEFAULT_SEED, Options, generate, write
from .validation import read_dir, validate


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="dataharbor_fixtures", description="DataHarbor source fixtures")
    commands = parser.add_subparsers(dest="command", required=True)

    gen = commands.add_parser("generate", help="write a fixture set into an empty directory")
    gen.add_argument("--out", type=Path, required=True, help="output directory (must be empty or missing)")
    gen.add_argument("--seed", type=int, default=DEFAULT_SEED)
    gen.add_argument(
        "--reference-date",
        type=date.fromisoformat,
        default=DEFAULT_REFERENCE_DATE,
        help="business dates are the --days dates before this one (YYYY-MM-DD)",
    )
    gen.add_argument("--days", type=int, default=DEFAULT_DAYS)
    gen.add_argument("--defects", action="store_true", help="inject the defects listed in the manifest")

    chk = commands.add_parser("check", help="validate a fixture set against its manifest")
    chk.add_argument("dir", type=Path)

    args = parser.parse_args(argv)

    if args.command == "generate":
        try:
            opts = Options(
                seed=args.seed, reference_date=args.reference_date, days=args.days, defects=args.defects
            )
        except ValueError as exc:
            parser.error(str(exc))
        fx = generate(opts)
        try:
            paths = write(fx, args.out)
        except FileExistsError as exc:
            parser.error(str(exc))
        for path in paths:
            print(path)
        records = sum(entry["record_count"] for entry in fx.manifest["files"])
        print(
            f"{len(fx.files)} files, {records} records, {len(fx.manifest['defects'])} defects (seed {opts.seed})",
            file=sys.stderr,
        )
        return 0

    findings = validate(read_dir(args.dir))
    for f in findings:
        print(f"{f.check}\t{f.file}\t{f.record or '-'}")
    print(f"{len(findings)} findings", file=sys.stderr)
    return 1 if findings else 0


if __name__ == "__main__":
    raise SystemExit(main())
