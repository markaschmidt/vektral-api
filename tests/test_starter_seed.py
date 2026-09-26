"""Initial Vite starter commit on GitHub-backed project create/link."""

from __future__ import annotations

import os
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

os.environ["ALLOW_DEV_BEARER"] = "true"
os.environ["VEKTRAL_USE_MEMORY_STORE"] = "true"
os.environ["FIREBASE_STUB_MODE"] = "true"
os.environ.pop("GOOGLE_APPLICATION_CREDENTIALS", None)

AUTH_A = {"Authorization": "Bearer dev:alice"}


def _connect_github(uid: str = "alice", token: str = "gho_test") -> None:
    from app.store import get_store

    store = get_store()
    store.github_save_pending(f"st-{uid}", uid)
    store.github_complete(
        f"st-{uid}",
        login=uid,
        access_token=token,
        scopes="repo",
    )


def _accessible_repo(full_name: str, token: str | None) -> dict:
    return {"full_name": full_name, "accessible": True, "reason": ""}


@pytest.fixture()
def client(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ALLOW_DEV_BEARER", "true")
    monkeypatch.setenv("VEKTRAL_USE_MEMORY_STORE", "true")
    monkeypatch.setenv("FIREBASE_STUB_MODE", "true")

    from app.agent_kv import clear_memory
    from app.config import get_settings
    from app.store import reset_memory_store, set_store_override

    get_settings.cache_clear()
    set_store_override(None)
    reset_memory_store()
    clear_memory()

    from app.main import app

    with TestClient(app) as c:
        yield c

    clear_memory()
    set_store_override(None)
    get_settings.cache_clear()


def test_create_without_repo_is_ready(client: TestClient):
    with patch(
        "app.services.preview.preview_runner_request",
        new_callable=AsyncMock,
    ) as runner:
        created = client.post(
            "/api/workspaces",
            headers=AUTH_A,
            json={"name": "No GitHub"},
        )
        assert created.status_code == 201
        runner.assert_not_called()
    assert created.json()["site_ready"] is True
    enter = client.post(
        f"/api/workspaces/{created.json()['id']}/enter",
        headers=AUTH_A,
    )
    assert enter.status_code == 200


def test_create_with_repo_seeds_starter(client: TestClient):
    _connect_github()
    runner = AsyncMock(
        return_value={
            "ok": True,
            "commit_sha": "abc123",
            "starter_seeded": True,
            "default_merged": True,
            "branch": "vektral/ws_test",
        }
    )
    with (
        patch("app.services.preview.github_token_for_user", return_value="gho_test"),
        patch(
            "app.services.workspaces.gh_repos.verify_repo_access",
            side_effect=_accessible_repo,
        ),
        patch("app.services.preview.preview_runner_request", runner),
    ):
        created = client.post(
            "/api/workspaces",
            headers=AUTH_A,
            json={"name": "With GitHub", "repo_full_name": "alice/demo"},
        )
    assert created.status_code == 201
    runner.assert_awaited_once()
    args, kwargs = runner.await_args
    assert args[0] == "POST"
    assert args[1] == "/v1/git/seed_starter"
    body = args[2]
    assert body["repo_full_name"] == "alice/demo"
    assert body["github_token"] == "gho_test"
    assert body["workspace_id"] == created.json()["id"]
    assert created.json()["site_ready"] is True
    assert created.json()["site_status"] == "ready"


def test_link_repo_on_patch_seeds_starter(client: TestClient):
    created = client.post(
        "/api/workspaces",
        headers=AUTH_A,
        json={"name": "Later GitHub"},
    )
    wid = created.json()["id"]
    _connect_github()
    runner = AsyncMock(
        return_value={"ok": True, "commit_sha": "def456", "starter_seeded": True}
    )
    with (
        patch("app.services.preview.github_token_for_user", return_value="gho_test"),
        patch(
            "app.services.workspaces.gh_repos.verify_repo_access",
            side_effect=_accessible_repo,
        ),
        patch("app.services.preview.preview_runner_request", runner),
    ):
        patched = client.patch(
            f"/api/workspaces/{wid}",
            headers=AUTH_A,
            json={"repo_full_name": "alice/later"},
        )
    assert patched.status_code == 200
    runner.assert_awaited_once()
    assert runner.await_args.args[1] == "/v1/git/seed_starter"
    assert runner.await_args.args[2]["repo_full_name"] == "alice/later"


def test_seed_failure_does_not_fail_create(client: TestClient):
    _connect_github()
    runner = AsyncMock(return_value={"ok": False, "error": "push denied"})
    with (
        patch("app.services.preview.github_token_for_user", return_value="gho_test"),
        patch(
            "app.services.workspaces.gh_repos.verify_repo_access",
            side_effect=_accessible_repo,
        ),
        patch("app.services.preview.preview_runner_request", runner),
    ):
        created = client.post(
            "/api/workspaces",
            headers=AUTH_A,
            json={"name": "Seed fail", "repo_full_name": "alice/fail"},
        )
        assert created.status_code == 201
        assert created.json()["repo_full_name"] == "alice/fail"
        assert created.json()["site_ready"] is False
        assert created.json()["site_status"] == "failed"

        blocked = client.post(
            f"/api/workspaces/{created.json()['id']}/enter",
            headers=AUTH_A,
        )
        assert blocked.status_code == 409
        assert blocked.json()["detail"]["error"] == "site_not_ready"


def test_create_with_expired_github_is_rejected(client: TestClient):
    from datetime import datetime, timedelta, timezone

    from app.store import get_store

    expired = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    store = get_store()
    store.github_save_pending("st-dead", "alice")
    store.github_complete(
        "st-dead",
        login="alice",
        access_token="ghu_old",
        scopes="repo",
        refresh_token="",
        expires_at=expired,
    )
    created = client.post(
        "/api/workspaces",
        headers=AUTH_A,
        json={"name": "plzwork2", "repo_full_name": "markaschmidt/plzwork"},
    )
    assert created.status_code == 401
    assert created.json()["detail"]["error"] == "github_reauth_required"
