import uuid

import pytest

from dataharbor_api.auth import hash_token
from dataharbor_api.migrations import upgrade
from dataharbor_api.models import ApiToken, User
from dataharbor_api.seed import seed_demo


@pytest.fixture
def demo(client, database_url):
    """Migrated database with the seeded demo workspace; returns tokens by role."""
    upgrade(database_url)
    with client.app.state.database.new_session() as session:
        tokens = seed_demo(session)
    by_role = dict(zip(("admin", "engineer", "viewer"), tokens.values(), strict=True))
    workspace = client.get("/v1/workspaces", headers=auth(by_role["viewer"])).json()[0]
    return {"tokens": by_role, "workspace_id": workspace["id"]}


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def add_user(client, email: str) -> tuple[uuid.UUID, str]:
    with client.app.state.database.new_session() as session:
        user = User(email=email, display_name=email.split("@")[0])
        session.add(user)
        session.flush()
        token = f"dh_test_{uuid.uuid4().hex}"
        session.add(ApiToken(user_id=user.id, token_hash=hash_token(token), label="test"))
        session.commit()
        return user.id, token


def test_missing_token_is_401_with_challenge(client, demo):
    response = client.get("/v1/workspaces")

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
    assert response.json()["error"]["code"] == "unauthenticated"


@pytest.mark.parametrize("header", ["Bearer dh_not-a-real-token", "Basic Zm9vOmJhcg==", "Bearer "])
def test_unknown_or_malformed_token_is_401(client, demo, header):
    response = client.get("/v1/workspaces", headers={"Authorization": header})

    assert response.status_code == 401


def test_revoked_token_is_401(client, demo):
    from datetime import UTC, datetime

    token = demo["tokens"]["viewer"]
    with client.app.state.database.new_session() as session:
        row = session.query(ApiToken).filter_by(token_hash=hash_token(token)).one()
        row.revoked_at = datetime.now(UTC)
        session.commit()

    assert client.get("/v1/workspaces", headers=auth(token)).status_code == 401


def test_only_the_token_hash_is_stored(client, demo):
    with client.app.state.database.new_session() as session:
        stored = {row.token_hash for row in session.query(ApiToken)}

    for token in demo["tokens"].values():
        assert token not in stored
        assert hash_token(token) in stored


def test_member_lists_their_workspaces_with_role(client, demo):
    for role, token in demo["tokens"].items():
        body = client.get("/v1/workspaces", headers=auth(token)).json()
        assert [(w["slug"], w["role"]) for w in body] == [("acme-analytics", role)]


def test_viewer_reads_workspace_and_members(client, demo):
    headers = auth(demo["tokens"]["viewer"])
    workspace_id = demo["workspace_id"]

    assert client.get(f"/v1/workspaces/{workspace_id}", headers=headers).status_code == 200
    members = client.get(f"/v1/workspaces/{workspace_id}/members", headers=headers).json()
    assert sorted(m["role"] for m in members) == ["admin", "engineer", "viewer"]


def test_non_member_gets_404_not_403(client, demo):
    _, outsider = add_user(client, "olivia.outsider@example.test")
    workspace_id = demo["workspace_id"]

    response = client.get(f"/v1/workspaces/{workspace_id}", headers=auth(outsider))
    assert response.status_code == 404
    assert client.get("/v1/workspaces", headers=auth(outsider)).json() == []
    unknown = client.get(f"/v1/workspaces/{uuid.uuid4()}", headers=auth(outsider))
    assert unknown.status_code == 404


@pytest.mark.parametrize("role", ["viewer", "engineer"])
def test_non_admin_cannot_manage_members(client, demo, role):
    new_user, _ = add_user(client, "nina.new@example.test")
    url = f"/v1/workspaces/{demo['workspace_id']}/members/{new_user}"
    headers = auth(demo["tokens"][role])

    response = client.put(url, json={"role": "viewer"}, headers=headers)
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "forbidden"
    assert client.delete(url, headers=headers).status_code == 403


def test_admin_adds_changes_and_removes_a_member(client, demo):
    new_user, new_token = add_user(client, "nina.new@example.test")
    workspace_id = demo["workspace_id"]
    url = f"/v1/workspaces/{workspace_id}/members/{new_user}"
    headers = auth(demo["tokens"]["admin"])

    added = client.put(url, json={"role": "viewer"}, headers=headers)
    assert added.status_code == 201
    assert added.json()["role"] == "viewer"
    assert client.get(f"/v1/workspaces/{workspace_id}", headers=auth(new_token)).status_code == 200

    changed = client.put(url, json={"role": "engineer"}, headers=headers)
    assert changed.status_code == 200
    assert changed.json()["role"] == "engineer"

    assert client.delete(url, headers=headers).status_code == 204
    assert client.get(f"/v1/workspaces/{workspace_id}", headers=auth(new_token)).status_code == 404
    assert client.delete(url, headers=headers).status_code == 404


def test_unknown_user_or_role_is_rejected(client, demo):
    base = f"/v1/workspaces/{demo['workspace_id']}/members"
    headers = auth(demo["tokens"]["admin"])

    assert client.put(f"{base}/{uuid.uuid4()}", json={"role": "viewer"}, headers=headers).status_code == 404
    new_user, _ = add_user(client, "nina.new@example.test")
    bad = client.put(f"{base}/{new_user}", json={"role": "owner"}, headers=headers)
    assert bad.status_code == 422
    assert "owner" not in bad.text


def test_last_admin_cannot_be_demoted_or_removed(client, demo):
    workspace_id = demo["workspace_id"]
    headers = auth(demo["tokens"]["admin"])
    members = client.get(f"/v1/workspaces/{workspace_id}/members", headers=headers).json()
    admin_id = next(m["user_id"] for m in members if m["role"] == "admin")
    url = f"/v1/workspaces/{workspace_id}/members/{admin_id}"

    assert client.put(url, json={"role": "viewer"}, headers=headers).status_code == 409
    assert client.delete(url, headers=headers).status_code == 409

    engineer_id = next(m["user_id"] for m in members if m["role"] == "engineer")
    promote = f"/v1/workspaces/{workspace_id}/members/{engineer_id}"
    assert client.put(promote, json={"role": "admin"}, headers=headers).status_code == 200
    assert client.put(url, json={"role": "viewer"}, headers=headers).status_code == 200


def test_create_workspace_makes_caller_admin(client, demo):
    headers = auth(demo["tokens"]["viewer"])

    created = client.post("/v1/workspaces", json={"slug": "victor-lab", "name": "Victor Lab"}, headers=headers)
    assert created.status_code == 201
    assert created.json()["role"] == "admin"

    duplicate = client.post("/v1/workspaces", json={"slug": "victor-lab", "name": "Again"}, headers=headers)
    assert duplicate.status_code == 409


@pytest.mark.parametrize(
    "body",
    [
        {"slug": "No Caps", "name": "x"},
        {"slug": "ab", "name": "x"},
        {"slug": "-leading", "name": "x"},
        {"slug": "valid-slug", "name": ""},
        {"slug": "valid-slug", "name": "x", "extra": 1},
    ],
)
def test_create_workspace_validation(client, demo, body):
    response = client.post("/v1/workspaces", json=body, headers=auth(demo["tokens"]["admin"]))

    assert response.status_code == 422


def test_seed_is_idempotent(client, demo):
    with client.app.state.database.new_session() as session:
        seed_demo(session)
        assert session.query(User).count() == 3

    members = client.get(
        f"/v1/workspaces/{demo['workspace_id']}/members", headers=auth(demo["tokens"]["viewer"])
    ).json()
    assert len(members) == 3


def test_seed_cli_prints_one_token_per_role(database_url, capsys):
    from dataharbor_api.seed import main

    upgrade(database_url)
    assert main(["--database-url", database_url]) == 0

    lines = capsys.readouterr().out.splitlines()
    assert lines[0] == "Workspace: acme-analytics"
    assert [line.split()[0] for line in lines[1:]] == ["admin", "engineer", "viewer"]
    assert all(line.split()[2].startswith("dh_") for line in lines[1:])

