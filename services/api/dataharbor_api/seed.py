"""Seed a demo workspace with one synthetic user per role and its datasets.

    python -m dataharbor_api.seed

Idempotent: re-running keeps existing users and memberships, and only
issues a new token for each demo user. Demo datasets mirror the
synthetic sources in `packages/fixtures` and are only added if missing. Tokens are printed once and only
their hashes are stored. All identities use the reserved `example.test`
domain; no real people or data.
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
    Layer,
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
