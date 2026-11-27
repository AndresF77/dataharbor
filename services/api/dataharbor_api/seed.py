"""Seed a demo workspace with one synthetic user per role and its datasets.

    python -m dataharbor_api.seed

Idempotent: re-running keeps existing users and memberships, and only
issues a new token for each demo user. Demo datasets mirror the
synthetic sources in `packages/fixtures` and are only added if missing;
the three raw datasets get a first ingestion contract describing the
fixture files, unless they already have one, and the raw-source ingestion
pipeline (`services/worker/dags/ingest_raw_sources.py`) is registered.
Each raw dataset gets a freshness rule on its landed files unless it has one,
and the data quality rules in `DEMO_QUALITY_RULES` that it lacks (by name).
Tokens are printed once and only their hashes are stored. All identities
use the reserved `example.test` domain; no real people or data.
"""

import argparse
import sys

from sqlalchemy import select
from sqlalchemy.orm import Session

from dataharbor_api.auth import hash_token, new_token
from dataharbor_api.config import get_settings
from dataharbor_api.db import Database
from dataharbor_api.models import (
    ApiToken,
    Dataset,
    FreshnessRule,
    FreshnessSignal,
    IngestionContract,
    Layer,
    Pipeline,
    PipelineKind,
    QualityRule,
    Role,
    User,
    Workspace,
    WorkspaceMember,
)

DEMO_WORKSPACE = ("acme-analytics", "Acme Analytics")
DEMO_USERS = [
    ("ada.admin@example.test", "Ada Admin", Role.ADMIN),
    ("erin.engineer@example.test", "Erin Engineer", Role.ENGINEER),
    ("victor.viewer@example.test", "Victor Viewer", Role.VIEWER),
]
# (name, layer, description, freshness SLA in minutes); owned by the engineer.
DEMO_DATASETS = [
    ("raw_customers", Layer.RAW, "Daily customer snapshot CSV from the synthetic CRM", 1440),
    ("raw_orders", Layer.RAW, "Daily orders CSV from the synthetic shop", 1440),
    ("raw_payments", Layer.RAW, "Daily payments JSONL from the synthetic payment provider", 1440),
    ("stg_orders", Layer.STAGING, "Typed, de-duplicated orders", 1500),
    ("stg_payments", Layer.STAGING, "Typed payments with orphan rows flagged", 1500),
    ("daily_revenue", Layer.MART, "Paid revenue per day and currency", 1560),
]


def _columns(*specs: str) -> list[dict]:
    """`"name:type"` or `"name:type?"` (nullable) -> contract column dicts."""
    columns = []
    for spec in specs:
        name, _, column_type = spec.partition(":")
        nullable = column_type.endswith("?")
        columns.append({"name": name, "type": column_type.rstrip("?"), "nullable": nullable})
    return columns


# First contract version for each raw dataset, matching packages/fixtures output.
DEMO_CONTRACTS = {
    "raw_customers": {
        "format": "csv",
        "columns": _columns(
            "customer_id:string", "customer_name:string", "email:string",
            "country:string", "segment:string", "signup_date:date",
        ),
        "primary_key": ["customer_id"],
    },
    "raw_orders": {
        "format": "csv",
        "columns": _columns(
            "order_id:string", "customer_id:string", "merchant:string", "product:string",
            "quantity:integer", "unit_price:decimal", "amount:decimal", "currency:string",
            "status:string", "ordered_at:timestamp",
        ),
        "primary_key": ["order_id"],
    },
    "raw_payments": {
        "format": "jsonl",
        "columns": _columns(
            "payment_id:string", "order_id:string", "method:string", "amount:decimal",
            "currency:string", "status:string", "paid_at:timestamp",
        ),
        "primary_key": ["payment_id"],
    },
}

# Matches DAG_ID and the schedule in services/worker/dags/ingest_raw_sources.py.
DEMO_PIPELINES = [
    (
        "ingest_raw_sources",
        PipelineKind.INGESTION,
        "dataharbor_ingest_raw_sources",
        "0 3 * * *",
        "Lands the daily customers, orders and payments files once they are due (03:00 UTC)",
    ),
]


# Raw files are due daily at 03:00 UTC: warn after 25 h, stale after 26 h without one.
DEMO_RAW_FRESHNESS = {"warn_after_minutes": 1500, "error_after_minutes": 1560}

# Value checks the contracts cannot express, matching packages/fixtures output:
# (dataset, name, check, columns, params, severity, description).
DEMO_QUALITY_RULES = [
    ("raw_customers", "has_customers", "row_count", [], {"min": "1"}, "error",
     "The daily snapshot is never empty"),
    ("raw_customers", "email_unique", "unique", ["email"], {}, "warn",
     "One customer per email address"),
    ("raw_customers", "segment_known", "accepted_values", ["segment"],
     {"values": ["consumer", "small_business"]}, "error", "Known customer segments"),
    ("raw_orders", "amount_non_negative", "value_range", ["amount"], {"min": "0"}, "error",
     "Order amounts are never negative"),
    ("raw_orders", "quantity_plausible", "value_range", ["quantity"], {"min": "1", "max": "100"},
     "warn", "Between 1 and 100 items per order line"),
    ("raw_orders", "status_known", "accepted_values", ["status"],
     {"values": ["completed", "cancelled"]}, "error", "Known order statuses"),
    ("raw_payments", "amount_positive", "value_range", ["amount"], {"min": "0.01"}, "error",
     "Captured payments have a positive amount"),
    ("raw_payments", "method_known", "accepted_values", ["method"],
     {"values": ["card", "bank_transfer", "wallet"]}, "error", "Known payment methods"),
    ("raw_payments", "currency_usd", "accepted_values", ["currency"], {"values": ["USD"]}, "warn",
     "The synthetic provider settles in USD only"),
]


def seed_demo(session: Session) -> dict[str, str]:
    """Create the demo workspace and users; return {email: new bearer token}."""
    slug, name = DEMO_WORKSPACE
    workspace = session.scalar(select(Workspace).where(Workspace.slug == slug))
    if workspace is None:
        workspace = Workspace(slug=slug, name=name)
        session.add(workspace)
        session.flush()

    tokens: dict[str, str] = {}
    for email, display_name, role in DEMO_USERS:
        user = session.scalar(select(User).where(User.email == email))
        if user is None:
            user = User(email=email, display_name=display_name)
            session.add(user)
            session.flush()
        if session.get(WorkspaceMember, (workspace.id, user.id)) is None:
            session.add(WorkspaceMember(workspace_id=workspace.id, user_id=user.id, role=role.value))
        token = new_token()
        session.add(ApiToken(user_id=user.id, token_hash=hash_token(token), label="demo seed"))
        tokens[email] = token

    owner = session.scalar(select(User).where(User.email == DEMO_USERS[1][0]))
    for name, layer, description, sla in DEMO_DATASETS:
        exists = session.scalar(
            select(Dataset.id).where(Dataset.workspace_id == workspace.id, Dataset.name == name)
        )
        if exists is None:
            session.add(
                Dataset(
                    workspace_id=workspace.id,
                    name=name,
                    layer=layer.value,
                    description=description,
                    owner_user_id=owner.id,
                    freshness_sla_minutes=sla,
                    version=1,
                )
            )
    session.flush()

    for name, contract in DEMO_CONTRACTS.items():
        dataset = session.scalar(
            select(Dataset).where(Dataset.workspace_id == workspace.id, Dataset.name == name)
        )
        has_contract = session.scalar(
            select(IngestionContract.id).where(IngestionContract.dataset_id == dataset.id)
        )
        if dataset.layer == Layer.RAW.value and has_contract is None:
            session.add(
                IngestionContract(
                    dataset_id=dataset.id, version=1, created_by_user_id=owner.id, **contract
                )
            )

    raw_datasets = session.scalars(
        select(Dataset).where(Dataset.workspace_id == workspace.id, Dataset.layer == Layer.RAW.value)
    ).all()
    for dataset in raw_datasets:
        has_rule = session.scalar(
            select(FreshnessRule.id).where(FreshnessRule.dataset_id == dataset.id)
        )
        if has_rule is None:
            session.add(
                FreshnessRule(
                    dataset_id=dataset.id,
                    signal=FreshnessSignal.LANDED_FILE.value,
                    version=1,
                    updated_by_user_id=owner.id,
                    **DEMO_RAW_FRESHNESS,
                )
            )

    raw_by_name = {d.name: d for d in raw_datasets}
    for dataset_name, name, check, columns, params, severity, description in DEMO_QUALITY_RULES:
        dataset = raw_by_name.get(dataset_name)
        if dataset is None:
            continue
        exists = session.scalar(
            select(QualityRule.id).where(
                QualityRule.dataset_id == dataset.id, QualityRule.name == name
            )
        )
        if exists is None:
            session.add(
                QualityRule(
                    dataset_id=dataset.id,
                    name=name,
                    check_kind=check,
                    columns=columns,
                    params=params,
                    severity=severity,
                    description=description,
                    enabled=True,
                    version=1,
                    updated_by_user_id=owner.id,
                )
            )

    for name, kind, dag_id, schedule, description in DEMO_PIPELINES:
        exists = session.scalar(
            select(Pipeline.id).where(Pipeline.workspace_id == workspace.id, Pipeline.name == name)
        )
        if exists is None:
            session.add(
                Pipeline(
                    workspace_id=workspace.id,
                    name=name,
                    kind=kind.value,
                    dag_id=dag_id,
                    schedule=schedule,
                    description=description,
                )
            )
    session.commit()
    return tokens


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--database-url", default=None, help="defaults to DATAHARBOR_DATABASE_URL")
    args = parser.parse_args(argv)

    database = Database(args.database_url or get_settings().database_url)
    try:
        with database.new_session() as session:
            tokens = seed_demo(session)
    finally:
        database.dispose()

    print(f"Workspace: {DEMO_WORKSPACE[0]}")
    for (email, _, role), token in zip(DEMO_USERS, tokens.values(), strict=True):
        print(f"{role.value:<9} {email:<28} {token}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
