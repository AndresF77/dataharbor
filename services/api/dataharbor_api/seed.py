"""Seed a demo workspace with one synthetic user per role.

    python -m dataharbor_api.seed

Idempotent: re-running keeps existing users and memberships, and only
issues a new token for each demo user. Tokens are printed once and only
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
from dataharbor_api.models import ApiToken, Role, User, Workspace, WorkspaceMember

DEMO_WORKSPACE = ("acme-analytics", "Acme Analytics")
DEMO_USERS = [
    ("ada.admin@example.test", "Ada Admin", Role.ADMIN),
    ("erin.engineer@example.test", "Erin Engineer", Role.ENGINEER),
    ("victor.viewer@example.test", "Victor Viewer", Role.VIEWER),
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
