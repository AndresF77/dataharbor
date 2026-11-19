import uuid

import pytest

from dataharbor_api.datasets import encode_cursor
from dataharbor_api.models import Dataset
from dataharbor_api.seed import DEMO_DATASETS, seed_demo
from tests.helpers import add_user, auth


def url(demo, suffix: str = "") -> str:
    return f"/v1/workspaces/{demo['workspace_id']}/datasets{suffix}"


def new_dataset(**overrides) -> dict:
    body = {"name": "stg_customers", "layer": "staging", "description": "Typed customers"}
    body.update(overrides)
    return body


def member_id(client, demo, role: str) -> str:
    members = client.get(
        f"/v1/workspaces/{demo['workspace_id']}/members", headers=auth(demo["tokens"]["admin"])
    ).json()
    return next(m["user_id"] for m in members if m["role"] == role)


def test_viewer_lists_seeded_datasets_by_name(client, demo):
    response = client.get(url(demo), headers=auth(demo["tokens"]["viewer"]))

    assert response.status_code == 200
    body = response.json()
    assert [d["name"] for d in body["items"]] == sorted(name for name, *_ in DEMO_DATASETS)
    assert body["next_cursor"] is None
    assert all(d["version"] == 1 and d["owner_user_id"] for d in body["items"])


def test_list_filters_by_layer(client, demo):
    response = client.get(url(demo), params={"layer": "raw"}, headers=auth(demo["tokens"]["viewer"]))

    assert [d["name"] for d in response.json()["items"]] == [
        "raw_customers",
        "raw_orders",
        "raw_payments",
    ]


def test_cursor_pagination_walks_every_dataset_once(client, demo):
    headers = auth(demo["tokens"]["viewer"])
    seen, cursor = [], None
    for _ in range(10):
        params = {"limit": 4} | ({"cursor": cursor} if cursor else {})
        page = client.get(url(demo), params=params, headers=headers).json()
        seen += [d["name"] for d in page["items"]]
        cursor = page["next_cursor"]
        if cursor is None:
            break

    assert seen == sorted(name for name, *_ in DEMO_DATASETS)


@pytest.mark.parametrize(
    "params", [{"limit": 0}, {"limit": 201}, {"layer": "bronze"}, {"cursor": "%%%"}, {"cursor": ""}]
)
def test_list_rejects_bad_query(client, demo, params):
    response = client.get(url(demo), params=params, headers=auth(demo["tokens"]["viewer"]))

    assert response.status_code == 422


def test_engineer_registers_dataset(client, demo):
    owner = member_id(client, demo, "viewer")
    body = new_dataset(owner_user_id=owner, freshness_sla_minutes=90)

    created = client.post(url(demo), json=body, headers=auth(demo["tokens"]["engineer"]))

    assert created.status_code == 201
    dataset = created.json()
    assert dataset["name"] == "stg_customers"
    assert dataset["owner_user_id"] == owner
    assert dataset["freshness_sla_minutes"] == 90
    assert dataset["version"] == 1
    detail = client.get(url(demo, f"/{dataset['id']}"), headers=auth(demo["tokens"]["viewer"]))
    assert detail.json() == dataset


def test_viewer_cannot_register_or_edit(client, demo):
    headers = auth(demo["tokens"]["viewer"])
    dataset = client.get(url(demo), headers=headers).json()["items"][0]

    assert client.post(url(demo), json=new_dataset(), headers=headers).status_code == 403
    edit = client.patch(url(demo, f"/{dataset['id']}"), json={"version": 1}, headers=headers)
    assert edit.status_code == 403


def test_duplicate_name_is_409(client, demo):
    response = client.post(
        url(demo), json=new_dataset(name="raw_orders", layer="raw"), headers=auth(demo["tokens"]["engineer"])
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "conflict"


@pytest.mark.parametrize(
    "overrides",
    [
        {"name": "Raw Orders"},
        {"name": "1orders"},
        {"name": "x"},
        {"name": "a" * 64},
        {"layer": "bronze"},
        {"description": "x" * 2001},
        {"freshness_sla_minutes": 0},
        {"freshness_sla_minutes": 525_601},
        {"extra": True},
    ],
)
def test_create_validation(client, demo, overrides):
    response = client.post(url(demo), json=new_dataset(**overrides), headers=auth(demo["tokens"]["engineer"]))

    assert response.status_code == 422


def test_owner_must_be_a_workspace_member(client, demo):
    outsider, _ = add_user(client, "olivia.outsider@example.test")
    headers = auth(demo["tokens"]["engineer"])

    for owner in (str(outsider), str(uuid.uuid4())):
        response = client.post(url(demo), json=new_dataset(owner_user_id=owner), headers=headers)
        assert response.status_code == 422


def test_datasets_are_scoped_to_their_workspace(client, demo):
    _, outsider_token = add_user(client, "olivia.outsider@example.test")
    other = client.post(
        "/v1/workspaces", json={"slug": "olivia-lab", "name": "Olivia Lab"}, headers=auth(outsider_token)
    ).json()
    dataset = client.get(url(demo), headers=auth(demo["tokens"]["viewer"])).json()["items"][0]

    # Not a member of the demo workspace: 404, not 403.
    assert client.get(url(demo), headers=auth(outsider_token)).status_code == 404
    # A dataset ID from another workspace is not found through one's own workspace.
    foreign = client.get(
        f"/v1/workspaces/{other['id']}/datasets/{dataset['id']}", headers=auth(outsider_token)
    )
    assert foreign.status_code == 404
    assert client.get(f"/v1/workspaces/{other['id']}/datasets", headers=auth(outsider_token)).json() == {
        "items": [],
        "next_cursor": None,
    }


def test_update_bumps_version_and_rejects_stale_edits(client, demo):
    headers = auth(demo["tokens"]["engineer"])
    dataset = client.get(url(demo), params={"layer": "mart"}, headers=headers).json()["items"][0]
    target = url(demo, f"/{dataset['id']}")

    first = client.patch(target, json={"version": 1, "freshness_sla_minutes": 120}, headers=headers)
    assert first.status_code == 200
    assert first.json()["version"] == 2
    assert first.json()["freshness_sla_minutes"] == 120
    assert first.json()["description"] == dataset["description"]

    stale = client.patch(target, json={"version": 1, "description": "Lost update"}, headers=headers)
    assert stale.status_code == 409
    assert client.get(target, headers=headers).json()["description"] == dataset["description"]


def test_update_can_clear_owner_and_sla(client, demo):
    headers = auth(demo["tokens"]["engineer"])
    dataset = client.get(url(demo), headers=headers).json()["items"][0]

    response = client.patch(
        url(demo, f"/{dataset['id']}"),
        json={"version": 1, "owner_user_id": None, "freshness_sla_minutes": None},
        headers=headers,
    )

    assert response.status_code == 200
    assert response.json()["owner_user_id"] is None
    assert response.json()["freshness_sla_minutes"] is None


@pytest.mark.parametrize(
    "body",
    [
        {"description": "no version"},
        {"version": 1, "layer": None},
        {"version": 1, "description": None},
        {"version": 1, "name": "renamed"},
        {"version": 0},
    ],
)
def test_update_validation(client, demo, body):
    headers = auth(demo["tokens"]["engineer"])
    dataset = client.get(url(demo), headers=headers).json()["items"][0]

    response = client.patch(url(demo, f"/{dataset['id']}"), json=body, headers=headers)

    assert response.status_code == 422


def test_unknown_dataset_is_404(client, demo):
    headers = auth(demo["tokens"]["engineer"])
    missing = url(demo, f"/{uuid.uuid4()}")

    assert client.get(missing, headers=headers).status_code == 404
    assert client.patch(missing, json={"version": 1}, headers=headers).status_code == 404


def test_removing_a_member_clears_their_dataset_ownership(client, demo):
    engineer = member_id(client, demo, "engineer")
    admin_headers = auth(demo["tokens"]["admin"])

    removed = client.delete(f"/v1/workspaces/{demo['workspace_id']}/members/{engineer}", headers=admin_headers)
    assert removed.status_code == 204

    items = client.get(url(demo), headers=admin_headers).json()["items"]
    assert all(d["owner_user_id"] is None and d["version"] == 2 for d in items)


def test_seed_does_not_duplicate_datasets(client, demo):
    with client.app.state.database.new_session() as session:
        seed_demo(session)
        assert session.query(Dataset).count() == len(DEMO_DATASETS)


def test_cursor_round_trips():
    assert encode_cursor("stg_orders").isascii()
    assert "=" not in encode_cursor("ab")
