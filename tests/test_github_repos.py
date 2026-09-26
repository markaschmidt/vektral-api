"""GitHub repo listing + OAuth refresh (expiring GitHub App user tokens)."""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

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
    monkeypatch.setenv("GITHUB_CLIENT_ID", "github-client")
    monkeypatch.setenv("GITHUB_CLIENT_SECRET", "github-secret")

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


class FakeResponse:
    def __init__(self, status: int, payload) -> None:
        self.status_code = status
        self._payload = payload
        self.text = json.dumps(payload)
        self.content = self.text.encode()

    def json(self):
        return self._payload


def _connect_github(
    *,
    access: str = "ghu_old",
    refresh: str = "ghr_refresh",
    expires_at: str = "",
) -> None:
    from app.store import get_store

    store = get_store()
    store.github_save_pending("st-gh", "alice")
    store.github_complete(
        "st-gh",
        login="alice",
        access_token=access,
        scopes="repo",
        refresh_token=refresh,
        expires_at=expires_at,
    )


def test_github_repos_empty_when_disconnected(client: TestClient):
    res = client.get("/api/github/repos", headers=AUTH_A)
    assert res.status_code == 200
    assert res.json() == []


def test_github_repos_refreshes_expired_token(client: TestClient):
    expired = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    _connect_github(expires_at=expired)

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def post(self, url, **kwargs):
            assert "login/oauth/access_token" in url
            return FakeResponse(
                200,
                {
                    "access_token": "ghu_new",
                    "refresh_token": "ghr_new",
                    "expires_in": 28800,
                },
            )

        def get(self, url, **kwargs):
            headers = kwargs.get("headers") or {}
            assert headers.get("Authorization") == "Bearer ghu_new"
            return FakeResponse(
                200,
                [
                    {
                        "full_name": "alice/demo",
                        "html_url": "https://github.com/alice/demo",
                        "default_branch": "main",
                        "private": False,
                        "description": "n",
                        "pushed_at": "2026-01-01T00:00:00Z",
                        "updated_at": "2026-01-01T00:00:00Z",
                        "language": "Python",
                        "stargazers_count": 3,
                    }
                ],
            )

    with (
        patch("app.services.oauth_refresh.httpx.Client", FakeClient),
        patch("app.services.github_repos.httpx.Client", FakeClient),
    ):
        res = client.get("/api/github/repos?sort=pushed&page=1", headers=AUTH_A)

    assert res.status_code == 200
    body = res.json()
    assert body[0]["full_name"] == "alice/demo"
    from app.store import get_store

    assert get_store().github_access_token("alice") == "ghu_new"
    assert get_store().github_oauth_secrets("alice")["refresh_token"] == "ghr_new"


def test_github_repos_expired_without_refresh_is_401_not_502(client: TestClient):
    expired = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    _connect_github(refresh="", expires_at=expired)
    res = client.get("/api/github/repos", headers=AUTH_A)
    assert res.status_code == 401
    assert res.json()["detail"]["error"] == "github_reauth_required"


def test_github_repos_bad_credentials_is_401_not_502(client: TestClient):
    _connect_github(refresh="", expires_at="")

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def get(self, url, **kwargs):
            return FakeResponse(401, {"message": "Bad credentials"})

    with patch("app.services.github_repos.httpx.Client", FakeClient):
        res = client.get("/api/github/repos", headers=AUTH_A)
    assert res.status_code == 401
    assert res.status_code != 502
    assert res.json()["detail"]["error"] == "github_reauth_required"


def test_github_status_flags_needs_reauth(client: TestClient):
    from datetime import datetime, timedelta, timezone

    expired = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    _connect_github(refresh="", expires_at=expired)
    res = client.get("/api/github/status", headers=AUTH_A)
    assert res.status_code == 200
    data = res.json()
    assert data["connected"] is True
    assert data["needs_reauth"] is True
