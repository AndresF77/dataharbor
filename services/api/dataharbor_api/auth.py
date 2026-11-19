"""Bearer-token authentication and workspace role checks.

Every workspace route resolves the caller's membership server-side:
- no or unknown token          -> 401
- not a member of the workspace -> 404 (existence is not revealed)
- member with too low a role    -> 403
"""

import hashlib
import secrets
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass

from fastapi import Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from dataharbor_api.models import ApiToken, Role, User, Workspace, WorkspaceMember

TOKEN_PREFIX = "dh_"
_UNAUTHENTICATED = {"WWW-Authenticate": "Bearer"}


def new_token() -> str:
    """A fresh bearer token. Show it once; store only `hash_token(token)`."""
    return TOKEN_PREFIX + secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def get_session(request: Request) -> Iterator[Session]:
    yield from request.app.state.database.session()


def _bearer_token(request: Request) -> str | None:
    scheme, _, token = request.headers.get("authorization", "").partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        return None
    return token.strip()


def current_user(request: Request, session: Session = Depends(get_session)) -> User:
    token = _bearer_token(request)
    if token is None:
        raise HTTPException(401, "Missing bearer token", headers=_UNAUTHENTICATED)
    user = session.scalar(
        select(User)
        .join(ApiToken, ApiToken.user_id == User.id)
        .where(ApiToken.token_hash == hash_token(token), ApiToken.revoked_at.is_(None))
    )
    if user is None:
        raise HTTPException(401, "Invalid or revoked token", headers=_UNAUTHENTICATED)
    request.state.user_id = str(user.id)
    return user


@dataclass(frozen=True)
class WorkspaceAccess:
    workspace: Workspace
    user: User
    role: Role


def require_role(minimum: Role) -> Callable[..., WorkspaceAccess]:
    """Dependency for `/v1/workspaces/{workspace_id}/...` routes."""

    def dependency(
        workspace_id: uuid.UUID,
        user: User = Depends(current_user),
        session: Session = Depends(get_session),
    ) -> WorkspaceAccess:
        row = session.execute(
            select(Workspace, WorkspaceMember.role)
            .join(WorkspaceMember, WorkspaceMember.workspace_id == Workspace.id)
            .where(Workspace.id == workspace_id, WorkspaceMember.user_id == user.id)
        ).one_or_none()
        if row is None:
            raise HTTPException(404, "Workspace not found")
        workspace, role_value = row
        role = Role(role_value)
        if not role.includes(minimum):
            raise HTTPException(403, f"Requires the {minimum.value} role in this workspace")
        return WorkspaceAccess(workspace=workspace, user=user, role=role)

    return dependency
