"""Linear OAuth, store, Vocal Bridge routing, and GraphQL action tests."""

from __future__ import annotations

import os
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

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


def _connect_linear(uid: str = "alice") -> None:
    from app.store import get_store

    store = get_store()
    store.linear_save_pending("st-linear", uid)
    store.linear_complete(
        "st-linear",
        user_id="user_1",
        name="Alice",
        email="alice@dev.local",
        access_token="lin_token",
        scopes="read,write,issues:create",
    )


def test_linear_status_disconnected(client: TestClient):
    assert client.get("/api/linear/status").status_code == 401
    res = client.get("/api/linear/status", headers=AUTH_A)
    assert res.status_code == 200
    data = res.json()
    assert data["connected"] is False
    assert data["configured"] is False

    connect = client.post("/api/linear/connect", headers=AUTH_A)
    assert connect.status_code == 200
    assert connect.json()["url"] == ""
    assert "not configured" in connect.json()["message"].lower()


def test_session_includes_linear(client: TestClient):
    sess = client.get("/api/session", headers=AUTH_A)
    assert sess.status_code == 200
    data = sess.json()
    assert data["linear"]["connected"] is False


def test_linear_store_connect_defaults_disconnect(client: TestClient):
    from app.store import get_store

    store = get_store()
    store.linear_save_pending("st", "alice")
    done = store.linear_complete(
        "st",
        user_id="u1",
        name="Alice",
        email="a@x",
        access_token="tok_secret",
    )
    assert done is not None
    assert done["connected"] is True
    assert done["linear_name"] == "Alice"
    assert store.linear_access_token("alice") == "tok_secret"

    defaults = store.linear_set_defaults(
        "alice", organization_id="org1", team_id="team1"
    )
    assert defaults is not None
    assert defaults["default_organization_id"] == "org1"
    assert defaults["default_team_id"] == "team1"

    status = client.get("/api/linear/status", headers=AUTH_A).json()
    assert status["connected"] is True
    assert status["default_team_id"] == "team1"

    saved = client.post(
        "/api/linear/defaults",
        headers=AUTH_A,
        json={"organization_id": "", "team_id": "team-personal"},
    )
    assert saved.status_code == 200
    assert saved.json()["default_organization_id"] == ""
    assert saved.json()["default_team_id"] == "team-personal"

    store.linear_disconnect("alice")
    assert store.linear_status("alice")["connected"] is False
    assert store.linear_access_token("alice") == ""


def test_linear_organizations_include_personal_and_org(client: TestClient):
    _connect_linear()
    fake = [
        {"id": "", "name": "Personal", "url_key": "", "scope": "personal"},
        {
            "id": "org1",
            "name": "Acme Labs",
            "url_key": "acme-labs",
            "scope": "organization",
        },
    ]
    with patch(
        "app.routers.linear.linear_svc.list_organizations",
        new=AsyncMock(return_value=fake),
    ):
        res = client.get("/api/linear/organizations", headers=AUTH_A)
    assert res.status_code == 200
    body = res.json()
    assert body[0]["scope"] == "personal"
    assert body[0]["id"] == ""
    assert any(row["id"] == "org1" for row in body)


def test_linear_teams_empty_when_disconnected(client: TestClient):
    res = client.get("/api/linear/teams", headers=AUTH_A)
    assert res.status_code == 200
    assert res.json() == []


def test_linear_projects_empty_when_disconnected(client: TestClient):
    res = client.get("/api/linear/projects", headers=AUTH_A)
    assert res.status_code == 200
    assert res.json() == []


def test_linear_projects_lists_when_connected(client: TestClient):
    _connect_linear()
    fake = [
        {
            "id": "proj1",
            "name": "Platform",
            "description": "Core",
            "url": "https://linear.app/acme/project/platform",
            "state": "started",
            "team_id": "team1",
            "team_name": "Engineering",
            "team_key": "ENG",
            "teams": [{"id": "team1", "name": "Engineering", "key": "ENG"}],
        }
    ]
    with patch(
        "app.routers.linear.linear_svc.list_projects",
        new=AsyncMock(return_value=fake),
    ):
        res = client.get("/api/linear/projects", headers=AUTH_A)
    assert res.status_code == 200
    body = res.json()
    assert len(body) == 1
    assert body[0]["id"] == "proj1"
    assert body[0]["team_key"] == "ENG"


def test_linear_projects_create_requires_connection(client: TestClient):
    res = client.post(
        "/api/linear/projects",
        headers=AUTH_A,
        json={"name": "New Platform"},
    )
    assert res.status_code == 400
    assert res.json()["detail"]["error"] == "linear_not_connected"


def test_linear_projects_create_when_connected(client: TestClient):
    _connect_linear()
    fake = {
        "id": "proj-new",
        "name": "New Platform",
        "description": "Paired with Vektral",
        "url": "https://linear.app/acme/project/new-platform",
        "state": "planned",
        "team_id": "team1",
        "team_name": "Engineering",
        "team_key": "ENG",
        "teams": [{"id": "team1", "name": "Engineering", "key": "ENG"}],
    }
    with patch(
        "app.routers.linear.linear_svc.create_project",
        new=AsyncMock(return_value=fake),
    ):
        res = client.post(
            "/api/linear/projects",
            headers=AUTH_A,
            json={"name": "New Platform", "description": "Paired with Vektral"},
        )
    assert res.status_code == 201
    body = res.json()
    assert body["id"] == "proj-new"
    assert body["team_key"] == "ENG"


def test_is_linear_command_detection():
    from app.services.linear_jobs import is_linear_command
    from app.services.research import is_research_command

    assert is_linear_command("create a Linear issue for the login bug")
    assert is_linear_command("archive ticket ENG-12")
    assert is_linear_command("add two tickets to the backlog")
    assert is_linear_command("create an issue titled Voice HUD")
    assert not is_linear_command("fix the navbar padding")
    assert not is_linear_command("")
    research = "research https://example.com"
    assert is_research_command(research)
    assert not is_linear_command(research)


def test_vocalbridge_linear_routes_before_coding(client: TestClient):
    res = client.post(
        "/api/vocalbridge/query",
        headers=AUTH_A,
        json={"text": "create a Linear issue for the login bug"},
    )
    assert res.status_code == 200
    body = res.json()
    assert body["command_type"] == "linear"
    assert "connect Linear" in (body.get("error") or "")


def test_vocalbridge_coding_not_stolen_by_linear(client: TestClient):
    res = client.post(
        "/api/vocalbridge/query",
        headers=AUTH_A,
        json={
            "text": "change the hero headline on the landing page",
            "workspace_id": "missing",
        },
    )
    assert res.status_code == 200
    body = res.json()
    assert body["command_type"] != "linear"


def test_execute_create_update_archive():
    import asyncio
    from unittest.mock import AsyncMock

    from app.services import linear_jobs

    async def _run() -> None:
        linear_jobs.linear_svc.create_issue = AsyncMock(  # type: ignore[method-assign]
            return_value={"id": "iss1", "identifier": "ENG-1", "title": "Login bug"}
        )
        linear_jobs.linear_svc.resolve_issue = AsyncMock(  # type: ignore[method-assign]
            return_value={
                "id": "iss1",
                "identifier": "ENG-1",
                "title": "Login bug",
                "team_id": "team1",
            }
        )
        linear_jobs.linear_svc.update_issue = AsyncMock(  # type: ignore[method-assign]
            return_value={
                "id": "iss1",
                "identifier": "ENG-1",
                "title": "Login bug fixed",
            }
        )
        linear_jobs.linear_svc.archive_issue = AsyncMock(return_value=True)  # type: ignore[method-assign]
        linear_jobs.linear_svc.resolve_state_id = AsyncMock(  # type: ignore[method-assign]
            return_value="state_done"
        )

        created = await linear_jobs._execute_action(
            "tok",
            {"op": "create", "title": "Login bug"},
            default_team_id="team1",
            viewer_id="user_1",
        )
        assert created["ok"] is True
        assert created["issue"]["identifier"] == "ENG-1"

        updated = await linear_jobs._execute_action(
            "tok",
            {"op": "update", "identifier": "ENG-1", "state": "Done"},
            default_team_id="team1",
            viewer_id="user_1",
        )
        assert updated["ok"] is True

        archived = await linear_jobs._execute_action(
            "tok",
            {"op": "delete", "identifier": "ENG-1"},
            default_team_id="team1",
            viewer_id="user_1",
        )
        assert archived["ok"] is True
        assert archived["op"] == "archive"

    asyncio.run(_run())


def test_run_linear_job_executes_plan(client: TestClient):
    import asyncio
    from unittest.mock import AsyncMock

    from app.agent_kv import put_doc
    from app.services import linear_jobs
    from app.store import get_store

    store = get_store()
    store.linear_save_pending("st-job", "alice")
    store.linear_complete(
        "st-job",
        user_id="user_1",
        name="Alice",
        email="a@x",
        access_token="tok",
    )
    store.linear_set_defaults("alice", organization_id="org1", team_id="team1")

    job = {
        "id": "job_lin_1",
        "command_text": "create a Linear issue called Login bug and archive ENG-9",
        "model": "",
        "logs": "",
    }
    put_doc("jobs", job["id"], job)

    async def _run() -> dict:
        with patch.object(
            linear_jobs,
            "_llm_chat",
            new=AsyncMock(
                return_value={
                    "ok": True,
                    "content": """{
  "summary": "created and archived",
  "actions": [
    {"op": "create", "title": "Login bug"},
    {"op": "archive", "identifier": "ENG-9"}
  ],
  "done": true
}""",
                }
            ),
        ), patch.object(
            linear_jobs.linear_svc,
            "recent_issues",
            new=AsyncMock(return_value=[]),
        ), patch.object(
            linear_jobs.linear_svc,
            "create_issue",
            new=AsyncMock(
                return_value={
                    "id": "iss1",
                    "identifier": "ENG-10",
                    "title": "Login bug",
                }
            ),
        ), patch.object(
            linear_jobs.linear_svc,
            "resolve_issue",
            new=AsyncMock(
                return_value={
                    "id": "iss9",
                    "identifier": "ENG-9",
                    "title": "Old",
                    "team_id": "team1",
                }
            ),
        ), patch.object(
            linear_jobs.linear_svc,
            "archive_issue",
            new=AsyncMock(return_value=True),
        ):
            return await linear_jobs.run_linear_job(job, "alice")

    out = asyncio.run(_run())
    assert out["status"] == "ready"
    assert "ENG-10" in out["result_json"]
