"""Workspaces and their members: `/v1/workspaces`."""

import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from dataharbor_api.auth import WorkspaceAccess, current_user, get_session, require_role
from dataharbor_api.models import Dataset, Role, User, Workspace, WorkspaceMember

router = APIRouter(prefix="/v1/workspaces", tags=["workspaces"])


class WorkspaceCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    slug: str = Field(pattern=r"^[a-z0-9](?:[a-z0-9-]{1,38})[a-z0-9]$")
    name: str = Field(min_length=1, max_length=120)


class WorkspaceOut(BaseModel):
    id: uuid.UUID
    slug: str
    name: str
    role: Role
    created_at: datetime


class MemberOut(BaseModel):
    user_id: uuid.UUID
    email: str
    display_name: str
    role: Role


class MemberUpsert(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: Role


def _workspace_out(workspace: Workspace, role: Role) -> WorkspaceOut:
    return WorkspaceOut(
        id=workspace.id,
        slug=workspace.slug,
        name=workspace.name,
        role=role,
        created_at=workspace.created_at,
    )


@router.get("", response_model=list[WorkspaceOut])
def list_workspaces(
    user: User = Depends(current_user), session: Session = Depends(get_session)
) -> list[WorkspaceOut]:
    rows = session.execute(
        select(Workspace, WorkspaceMember.role)
        .join(WorkspaceMember, WorkspaceMember.workspace_id == Workspace.id)
        .where(WorkspaceMember.user_id == user.id)
        .order_by(Workspace.slug)
    ).all()
    return [_workspace_out(workspace, Role(role)) for workspace, role in rows]


@router.post("", response_model=WorkspaceOut, status_code=201)
def create_workspace(
    body: WorkspaceCreate,
    user: User = Depends(current_user),
    session: Session = Depends(get_session),
) -> WorkspaceOut:
    """Create a workspace; the caller becomes its first admin."""
    workspace = Workspace(slug=body.slug, name=body.name)
    session.add(workspace)
    try:
        session.flush()
        session.add(
            WorkspaceMember(workspace_id=workspace.id, user_id=user.id, role=Role.ADMIN.value)
        )
        session.commit()
    except IntegrityError:
        session.rollback()
        raise HTTPException(409, "A workspace with this slug already exists") from None
    session.refresh(workspace)
    return _workspace_out(workspace, Role.ADMIN)


@router.get("/{workspace_id}", response_model=WorkspaceOut)
def get_workspace(access: WorkspaceAccess = Depends(require_role(Role.VIEWER))) -> WorkspaceOut:
    return _workspace_out(access.workspace, access.role)


@router.get("/{workspace_id}/members", response_model=list[MemberOut])
def list_members(
    access: WorkspaceAccess = Depends(require_role(Role.VIEWER)),
    session: Session = Depends(get_session),
) -> list[MemberOut]:
    rows = session.execute(
        select(User, WorkspaceMember.role)
        .join(WorkspaceMember, WorkspaceMember.user_id == User.id)
        .where(WorkspaceMember.workspace_id == access.workspace.id)
        .order_by(User.email)
    ).all()
    return [
        MemberOut(user_id=u.id, email=u.email, display_name=u.display_name, role=Role(role))
        for u, role in rows
    ]


def _lock_admins(session: Session, workspace_id: uuid.UUID) -> int:
    """Count admins, locking their rows so concurrent demotions serialize."""
    admins = session.scalars(
        select(WorkspaceMember.user_id)
        .where(
            WorkspaceMember.workspace_id == workspace_id,
            WorkspaceMember.role == Role.ADMIN.value,
        )
        .with_for_update()
    ).all()
    return len(admins)


def _member(session: Session, workspace_id: uuid.UUID, user_id: uuid.UUID) -> WorkspaceMember | None:
    return session.get(WorkspaceMember, (workspace_id, user_id), with_for_update=True)


@router.put("/{workspace_id}/members/{user_id}", response_model=MemberOut)
def upsert_member(
    user_id: uuid.UUID,
    body: MemberUpsert,
    response: Response,
    access: WorkspaceAccess = Depends(require_role(Role.ADMIN)),
    session: Session = Depends(get_session),
) -> MemberOut:
    """Add a user to the workspace or change their role (admin only)."""
    user = session.get(User, user_id)
    if user is None:
        raise HTTPException(404, "User not found")

    workspace_id = access.workspace.id
    admin_count = _lock_admins(session, workspace_id)
    member = _member(session, workspace_id, user_id)
    if member is None:
        session.add(WorkspaceMember(workspace_id=workspace_id, user_id=user_id, role=body.role.value))
        response.status_code = 201
    else:
        if member.role == Role.ADMIN.value and body.role != Role.ADMIN and admin_count <= 1:
            raise HTTPException(409, "A workspace must keep at least one admin")
        member.role = body.role.value
    session.commit()
    return MemberOut(
        user_id=user.id, email=user.email, display_name=user.display_name, role=body.role
    )


@router.delete("/{workspace_id}/members/{user_id}", status_code=204)
def remove_member(
    user_id: uuid.UUID,
    access: WorkspaceAccess = Depends(require_role(Role.ADMIN)),
    session: Session = Depends(get_session),
) -> Response:
    workspace_id = access.workspace.id
    admin_count = _lock_admins(session, workspace_id)
    member = _member(session, workspace_id, user_id)
    if member is None:
        raise HTTPException(404, "Member not found")
    if member.role == Role.ADMIN.value and admin_count <= 1:
        raise HTTPException(409, "A workspace must keep at least one admin")
    # A former member no longer owns anything in this workspace.
    session.execute(
        update(Dataset)
        .where(Dataset.workspace_id == workspace_id, Dataset.owner_user_id == user_id)
        .values(owner_user_id=None, version=Dataset.version + 1)
    )
    session.delete(member)
    session.commit()
    return Response(status_code=204)

