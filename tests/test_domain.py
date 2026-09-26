"""Domain API tests — memory store + ALLOW_DEV_BEARER (no Firebase SA)."""

from __future__ import annotations

import os
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

# Configure before importing app modules that cache settings
os.environ["ALLOW_DEV_BEARER"] = "true"
os.environ["VEKTRAL_USE_MEMORY_STORE"] = "true"
os.environ["FIREBASE_STUB_MODE"] = "true"
os.environ.pop("GOOGLE_APPLICATION_CREDENTIALS", None)


@pytest.fixture()
def client(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ALLOW_DEV_BEARER", "true")
    monkeypatch.setenv("VEKTRAL_USE_MEMORY_STORE", "true")
    monkeypatch.setenv("FIREBASE_STUB_MODE", "true")

    from app.config import get_settings
    from app.store import reset_memory_store, set_store_override

    get_settings.cache_clear()
    set_store_override(None)
    reset_memory_store()

    from app.main import app

    with TestClient(app) as c:
        yield c

    set_store_override(None)
    get_settings.cache_clear()


AUTH_A = {"Authorization": "Bearer dev:alice"}
AUTH_B = {"Authorization": "Bearer dev:bob"}


def test_reject_anonymous(client: TestClient):
    assert client.get("/api/workspaces").status_code == 401
    assert client.get("/api/session").status_code == 401
    assert client.get("/api/orgs").status_code == 401
    assert client.get("/api/github/status").status_code == 401


def test_workspace_crud_flow(client: TestClient):
    empty = client.get("/api/workspaces", headers=AUTH_A)
    assert empty.status_code == 200
    assert empty.json() == []

    created = client.post(
        "/api/workspaces",
        headers=AUTH_A,
        json={"name": "Orbital Notes", "description": "demo"},
    )
    assert created.status_code == 201
    body = created.json()
    assert body["name"] == "Orbital Notes"
    assert body["description"] == "demo"
    assert body["is_personal"] is True
    assert body["owner_uid"] == "alice"
    wid = body["id"]

    listed = client.get("/api/workspaces", headers=AUTH_A)
    assert listed.status_code == 200
    assert len(listed.json()) == 1
    assert listed.json()[0]["id"] == wid

    got = client.get(f"/api/workspaces/{wid}", headers=AUTH_A)
    assert got.status_code == 200
    assert got.json()["name"] == "Orbital Notes"

    from app.store import get_store

    store = get_store()
    store.github_save_pending("st-crud", "alice")
    store.github_complete(
        "st-crud",
        login="alice",
        access_token="gho_test",
        scopes="repo",
    )
    with (
        patch(
            "app.services.workspaces.gh_repos.verify_repo_access",
            return_value={"full_name": "alice/orbital", "accessible": True, "reason": ""},
        ),
        patch(
            "app.services.preview.seed_starter_checkout",
            new_callable=AsyncMock,
        ),
    ):
        patched = client.patch(
            f"/api/workspaces/{wid}",
            headers=AUTH_A,
            json={"name": "Renamed", "repo_full_name": "alice/orbital"},
        )
    assert patched.status_code == 200
    assert patched.json()["name"] == "Renamed"
    assert patched.json()["repo_full_name"] == "alice/orbital"


def test_foreign_uid_cannot_read_workspace(client: TestClient):
    created = client.post(
        "/api/workspaces",
        headers=AUTH_A,
        json={"name": "Secret"},
    )
    wid = created.json()["id"]

    forbidden = client.get(f"/api/workspaces/{wid}", headers=AUTH_B)
    assert forbidden.status_code in (403, 404)

    bob_list = client.get("/api/workspaces", headers=AUTH_B)
    assert bob_list.json() == []


def test_orgs_create_list(client: TestClient):
    created = client.post(
        "/api/orgs",
        headers=AUTH_A,
        json={"name": "Acme Labs"},
    )
    assert created.status_code == 201
    org = created.json()
    assert org["name"] == "Acme Labs"
    assert org["my_role"] == "owner"

    listed = client.get("/api/orgs", headers=AUTH_A)
    assert len(listed.json()) == 1

    bob = client.get("/api/orgs", headers=AUTH_B)
    assert bob.json() == []


def test_github_status_disconnected(client: TestClient):
    res = client.get("/api/github/status", headers=AUTH_A)
    assert res.status_code == 200
    data = res.json()
    assert data["connected"] is False
    assert data["configured"] is False

    connect = client.post("/api/github/connect", headers=AUTH_A)
    assert connect.status_code == 200
    assert connect.json()["url"] == ""
    assert "not configured" in connect.json()["message"].lower()


def test_session_includes_workspaces_and_orgs(client: TestClient):
    client.post("/api/workspaces", headers=AUTH_A, json={"name": "WS1"})
    client.post("/api/orgs", headers=AUTH_A, json={"name": "Org1"})

    sess = client.get("/api/session", headers=AUTH_A)
    assert sess.status_code == 200
    data = sess.json()
    assert data["_meta"]["stub"] is False
    assert len(data["workspaces"]) == 1
    assert data["workspaces"][0]["name"] == "WS1"
    assert len(data["orgs"]) == 1
    assert data["profile"]["org_count"] == 1
    assert data["github"]["connected"] is False


def test_auth_me_dev_bearer(client: TestClient):
    res = client.get("/auth/me", headers=AUTH_A)
    assert res.status_code == 200
    assert res.json()["uid"] == "alice"
    assert res.json()["email"] == "alice@dev.local"


AUTH_C = {"Authorization": "Bearer dev:carol"}


def test_org_invite_accept_and_workspace_acl(client: TestClient):
    org = client.post(
        "/api/orgs",
        headers=AUTH_A,
        json={"name": "Shared Labs"},
    ).json()
    oid = org["id"]

    invite = client.post(
        f"/api/orgs/{oid}/invites",
        headers=AUTH_A,
        json={"email": "bob@dev.local", "role": "member"},
    )
    assert invite.status_code == 201
    invite_id = invite.json()["id"]

    pending = client.get("/api/orgs/invites/pending", headers=AUTH_B)
    assert pending.status_code == 200
    assert any(i["id"] == invite_id for i in pending.json())

    accepted = client.post(
        f"/api/orgs/invites/{invite_id}/accept",
        headers=AUTH_B,
    )
    assert accepted.status_code == 200
    assert accepted.json()["ok"] is True

    members = client.get(f"/api/orgs/{oid}/members", headers=AUTH_A)
    assert members.status_code == 200
    uids = {m["uid"] for m in members.json()}
    assert uids == {"alice", "bob"}

    ws = client.post(
        f"/api/orgs/{oid}/workspaces",
        headers=AUTH_A,
        json={"name": "Team Board"},
    )
    assert ws.status_code == 201
    wid = ws.json()["id"]
    assert ws.json()["org_id"] == oid
    assert ws.json()["is_personal"] is False

    bob_list = client.get("/api/workspaces", headers=AUTH_B)
    assert any(w["id"] == wid for w in bob_list.json())

    bob_get = client.get(f"/api/workspaces/{wid}", headers=AUTH_B)
    assert bob_get.status_code == 200

    bob_enter = client.post(f"/api/workspaces/{wid}/enter", headers=AUTH_B)
    assert bob_enter.status_code == 200

    carol_get = client.get(f"/api/workspaces/{wid}", headers=AUTH_C)
    assert carol_get.status_code in (403, 404)


def test_org_join_code(client: TestClient):
    org = client.post(
        "/api/orgs",
        headers=AUTH_A,
        json={"name": "Code Club"},
    ).json()
    oid = org["id"]

    forbidden = client.post(f"/api/orgs/{oid}/join-code", headers=AUTH_B)
    assert forbidden.status_code in (403, 404)

    gen = client.post(f"/api/orgs/{oid}/join-code", headers=AUTH_A)
    assert gen.status_code == 200
    code = gen.json()["join_code"]
    assert len(code) >= 6

    owner_view = client.get(f"/api/orgs/{oid}", headers=AUTH_A).json()
    assert owner_view["has_join_code"] is True
    assert owner_view["join_code"] == code

    joined = client.post(
        "/api/orgs/join",
        headers=AUTH_C,
        json={"code": code},
    )
    assert joined.status_code == 200
    assert joined.json()["ok"] is True

    carol_orgs = client.get("/api/orgs", headers=AUTH_C).json()
    assert any(o["id"] == oid for o in carol_orgs)

    members = client.get(f"/api/orgs/{oid}/members", headers=AUTH_C).json()
    carol = next(m for m in members if m["uid"] == "carol")
    assert carol["role"] == "member"


def test_member_cannot_invite(client: TestClient):
    org = client.post(
        "/api/orgs",
        headers=AUTH_A,
        json={"name": "Gate"},
    ).json()
    oid = org["id"]
    gen = client.post(f"/api/orgs/{oid}/join-code", headers=AUTH_A).json()
    client.post("/api/orgs/join", headers=AUTH_B, json={"code": gen["join_code"]})

    denied = client.post(
        f"/api/orgs/{oid}/invites",
        headers=AUTH_B,
        json={"email": "carol@dev.local", "role": "member"},
    )
    assert denied.status_code == 403


AUTH_C = {"Authorization": "Bearer dev:carol"}


def test_org_invite_accept_and_workspace_acl(client: TestClient):
    org = client.post(
        "/api/orgs",
        headers=AUTH_A,
        json={"name": "Shared Labs"},
    ).json()
    oid = org["id"]

    invite = client.post(
        f"/api/orgs/{oid}/invites",
        headers=AUTH_A,
        json={"email": "bob@dev.local", "role": "member"},
    )
    assert invite.status_code == 201
    invite_id = invite.json()["id"]

    pending = client.get("/api/orgs/invites/pending", headers=AUTH_B)
    assert pending.status_code == 200
    assert any(i["id"] == invite_id for i in pending.json())

    accepted = client.post(
        f"/api/orgs/invites/{invite_id}/accept",
        headers=AUTH_B,
    )
    assert accepted.status_code == 200
    assert accepted.json()["ok"] is True

    members = client.get(f"/api/orgs/{oid}/members", headers=AUTH_A)
    assert members.status_code == 200
    uids = {m["uid"] for m in members.json()}
    assert uids == {"alice", "bob"}

    ws = client.post(
        f"/api/orgs/{oid}/workspaces",
        headers=AUTH_A,
        json={"name": "Team Board"},
    )
    assert ws.status_code == 201
    wid = ws.json()["id"]
    assert ws.json()["org_id"] == oid
    assert ws.json()["is_personal"] is False

    bob_list = client.get("/api/workspaces", headers=AUTH_B)
    assert any(w["id"] == wid for w in bob_list.json())

    bob_get = client.get(f"/api/workspaces/{wid}", headers=AUTH_B)
    assert bob_get.status_code == 200

    bob_enter = client.post(f"/api/workspaces/{wid}/enter", headers=AUTH_B)
    assert bob_enter.status_code == 200

    carol_get = client.get(f"/api/workspaces/{wid}", headers=AUTH_C)
    assert carol_get.status_code in (403, 404)


def test_org_join_code(client: TestClient):
    org = client.post(
        "/api/orgs",
        headers=AUTH_A,
        json={"name": "Code Club"},
    ).json()
    oid = org["id"]

    forbidden = client.post(f"/api/orgs/{oid}/join-code", headers=AUTH_B)
    assert forbidden.status_code in (403, 404)

    gen = client.post(f"/api/orgs/{oid}/join-code", headers=AUTH_A)
    assert gen.status_code == 200
    code = gen.json()["join_code"]
    assert len(code) >= 6

    owner_view = client.get(f"/api/orgs/{oid}", headers=AUTH_A).json()
    assert owner_view["has_join_code"] is True
    assert owner_view["join_code"] == code

    joined = client.post(
        "/api/orgs/join",
        headers=AUTH_C,
        json={"code": code},
    )
    assert joined.status_code == 200
    assert joined.json()["ok"] is True

    carol_orgs = client.get("/api/orgs", headers=AUTH_C).json()
    assert any(o["id"] == oid for o in carol_orgs)

    members = client.get(f"/api/orgs/{oid}/members", headers=AUTH_C).json()
    carol = next(m for m in members if m["uid"] == "carol")
    assert carol["role"] == "member"


def test_member_cannot_invite(client: TestClient):
    org = client.post(
        "/api/orgs",
        headers=AUTH_A,
        json={"name": "Gate"},
    ).json()
    oid = org["id"]
    gen = client.post(f"/api/orgs/{oid}/join-code", headers=AUTH_A).json()
    client.post("/api/orgs/join", headers=AUTH_B, json={"code": gen["join_code"]})

    denied = client.post(
        f"/api/orgs/{oid}/invites",
        headers=AUTH_B,
        json={"email": "carol@dev.local", "role": "member"},
    )
    assert denied.status_code == 403
