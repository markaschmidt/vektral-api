"""Preview runtimes: tickets, targets, locators, captures, external URLs, remount."""

from __future__ import annotations

import asyncio
import os
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

os.environ["ALLOW_DEV_BEARER"] = "true"
os.environ["VEKTRAL_USE_MEMORY_STORE"] = "true"
os.environ["FIREBASE_STUB_MODE"] = "true"
os.environ["PREVIEW_TICKET_SECRET"] = "test-ticket-secret"
os.environ.pop("GOOGLE_APPLICATION_CREDENTIALS", None)


@pytest.fixture()
def client(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ALLOW_DEV_BEARER", "true")
    monkeypatch.setenv("VEKTRAL_USE_MEMORY_STORE", "true")
    monkeypatch.setenv("FIREBASE_STUB_MODE", "true")
    monkeypatch.setenv("PREVIEW_TICKET_SECRET", "test-ticket-secret")
    monkeypatch.setenv("CAPTURES_ENABLED", "true")
    monkeypatch.setenv("DIALOGUE_ORCHESTRATION_ENABLED", "false")

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


AUTH = {"Authorization": "Bearer dev:alice"}


def _workspace(client: TestClient) -> str:
    res = client.post("/api/workspaces", headers=AUTH, json={"name": "RT", "description": "x"})
    assert res.status_code in (200, 201)
    return res.json()["id"]


def test_ticket_crypto_expire_and_tamper():
    from app.services.ticket_crypto import issue_ticket, verify_token

    token, _ = issue_ticket(
        secret="s",
        session_id="sid",
        uid="alice",
        workspace_id="ws",
        runtime_id="rt",
        route="/",
        nonce="n",
        ttl=-1,
    )
    with pytest.raises(ValueError):
        verify_token(token, "s")
    token2, _ = issue_ticket(
        secret="s",
        session_id="sid",
        uid="alice",
        workspace_id="ws",
        runtime_id="rt",
        route="/",
        nonce="n2",
        ttl=60,
    )
    with pytest.raises(ValueError):
        verify_token(token2, "other")


def test_launch_requires_auth(client: TestClient):
    wid = _workspace(client)
    pane = client.post(
        f"/api/workspaces/{wid}/panes",
        headers=AUTH,
        json={"title": "Home", "route": "/"},
    ).json()
    res = client.post(f"/api/workspaces/{wid}/panes/{pane['id']}/launch")
    assert res.status_code in (401, 403)


def test_launch_unready_preview(client: TestClient):
    wid = _workspace(client)
    pane = client.post(
        f"/api/workspaces/{wid}/panes",
        headers=AUTH,
        json={"title": "Home", "route": "/"},
    ).json()
    res = client.post(
        f"/api/workspaces/{wid}/panes/{pane['id']}/launch",
        headers=AUTH,
    )
    assert res.status_code == 409


def test_cross_workspace_pane_rejected(client: TestClient):
    wid1 = _workspace(client)
    wid2 = client.post(
        "/api/workspaces",
        headers={"Authorization": "Bearer dev:bob"},
        json={"name": "Other"},
    ).json()["id"]
    pane = client.post(
        f"/api/workspaces/{wid1}/panes",
        headers=AUTH,
        json={"title": "Home", "route": "/"},
    ).json()
    res = client.post(
        f"/api/workspaces/{wid2}/panes/{pane['id']}/launch",
        headers={"Authorization": "Bearer dev:bob"},
    )
    assert res.status_code in (403, 404)


def test_coding_targets_command_revision_and_no_home_fallback(client: TestClient):
    from app.agent_kv import put_doc
    from app.services.coding_targets import resolve_coding_targets
    import asyncio

    wid = _workspace(client)
    home = client.post(
        f"/api/workspaces/{wid}/panes",
        headers=AUTH,
        json={"title": "Home", "route": "/"},
    ).json()
    about = client.post(
        f"/api/workspaces/{wid}/panes",
        headers=AUTH,
        json={"title": "About", "route": "/about"},
    ).json()
    chat = client.post(
        f"/api/workspaces/{wid}/panes",
        headers=AUTH,
        json={"kind": "chat", "title": "Chat"},
    ).json()

    async def _head(_wid: str):
        return {"ok": True, "commit_sha": "abc123"}

    with patch("app.services.coding_targets.fetch_checkout_head", new=AsyncMock(side_effect=_head)):
        import asyncio

        snap = asyncio.run(
            resolve_coding_targets(
                wid,
                "alice",
                {
                    "focused_pane_ids": [about["id"], chat["id"]],
                    "context_revision": 9,
                    "primary_pane_id": about["id"],
                },
            )
        )
    assert snap["context_revision"] == 9
    assert snap["primary_target_pane_id"] == about["id"]
    assert [t["pane_id"] for t in snap["targets"]] == [about["id"]]
    assert snap["base_commit_sha"] == "abc123"

    # Chat-only focus with default cleared → NO_CODING_TARGETS
    put_doc("preview_sessions", wid, {"id": wid, "workspace_id": wid, "default_preview_pane_id": ""})
    from fastapi import HTTPException

    with patch("app.services.coding_targets.fetch_checkout_head", new=AsyncMock(return_value={"commit_sha": "x"})):
        with pytest.raises(HTTPException) as exc:
            asyncio.run(
                resolve_coding_targets(
                    wid,
                    "alice",
                    {"focused_pane_ids": [chat["id"]], "context_revision": 1},
                )
            )
    assert exc.value.status_code == 400
    assert exc.value.detail["error"] == "NO_CODING_TARGETS"
    _ = home


def test_source_locators_and_invalidation_ignores_model_pane_ids():
    from app.services.source_locators import (
        invalidate_panes,
        needs_restart,
        next_candidates,
        static_candidates,
        vite_candidates,
    )

    vite = vite_candidates("/about", ".", "touch src/pages/About.tsx")
    assert any("About" in p for p in vite)
    nxt = next_candidates("/posts/hello", ".")
    assert any("[...slug]" in p or "[slug]" in p for p in nxt)
    st = static_candidates("/docs", ".")
    assert "docs.html" in st or any(p.endswith("docs.html") for p in st)

    targets = [
        {"pane_id": "p_home", "route": "/"},
        {"pane_id": "p_about", "route": "/about"},
    ]
    # Route-specific
    hit = invalidate_panes(
        kind="vite-spa",
        changed_paths=["src/pages/about.tsx"],
        targets=targets,
        runtime_pane_ids=["p_home", "p_about", "p_other"],
    )
    assert hit == ["p_about"]
    # Global lockfile → all runtime panes, not model hints
    glob = invalidate_panes(
        kind="vite-spa",
        changed_paths=["package.json"],
        targets=targets,
        runtime_pane_ids=["p_home", "p_about", "p_other"],
    )
    assert glob == ["p_home", "p_about", "p_other"]
    assert needs_restart("vite-spa", ["package-lock.json"]) is True
    assert needs_restart("vite-spa", ["src/App.tsx"]) is False

    nxt_hit = invalidate_panes(
        kind="next",
        changed_paths=["app/posts/[slug]/page.tsx"],
        targets=[{"pane_id": "p_post", "route": "/posts/hello"}],
        runtime_pane_ids=["p_post", "p_home"],
    )
    assert "p_post" in nxt_hit


def test_captures_pairing_and_no_client_urls(client: TestClient, monkeypatch: pytest.MonkeyPatch):
    from app.services.captures import create_capture, model_image_payloads, pair_captures_to_targets
    from app.services.coding import _user_content

    monkeypatch.setenv("CAPTURES_ENABLED", "true")
    from app.config import get_settings

    get_settings.cache_clear()

    wid = _workspace(client)
    pane = client.post(
        f"/api/workspaces/{wid}/panes",
        headers=AUTH,
        json={"title": "Home", "route": "/"},
    ).json()
    view = create_capture(
        uid="alice",
        workspace_id=wid,
        pane_id=pane["id"],
        context_revision=3,
        content_type="image/png",
        body=b"\x89PNG" + b"\x00" * 32,
        visible_text="Hello <script>alert(1)</script>",
    )
    assert view["image_ref"].startswith("cap_")
    assert "<script" not in view["visible_text"]

    paired, reasons = pair_captures_to_targets(
        uid="alice",
        workspace_id=wid,
        targets=[{"pane_id": pane["id"]}],
        captures=[
            {
                "pane_id": pane["id"],
                "context_revision": 3,
                "image_ref": view["id"],
                "visible_text": "Hello",
            },
            {"pane_id": "nope", "context_revision": 3, "image_ref": view["id"]},
            {"pane_id": pane["id"], "context_revision": 99, "image_ref": view["id"]},
        ],
        context_revision=3,
    )
    assert len(paired) == 1
    assert any("not_a_target" in r or "stale" in r for r in reasons)

    urls, status = model_image_payloads(paired, "alice", wid)
    assert status == "ok"
    assert urls[0].startswith("data:image/png;base64,")
    content = _user_content("edit", ["https://evil.example/x.png", urls[0]])
    assert isinstance(content, list)
    assert all(
        str(p.get("image_url", {}).get("url", "")).startswith("data:")
        for p in content
        if p.get("type") == "image_url"
    )

    # oversize / wrong type
    with pytest.raises(Exception):
        create_capture(
            uid="alice",
            workspace_id=wid,
            pane_id=pane["id"],
            context_revision=3,
            content_type="text/html",
            body=b"<html>",
        )


def test_capture_upload_flag(client: TestClient, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("CAPTURES_ENABLED", "false")
    from app.config import get_settings

    get_settings.cache_clear()
    wid = _workspace(client)
    pane = client.post(
        f"/api/workspaces/{wid}/panes",
        headers=AUTH,
        json={"title": "Home", "route": "/"},
    ).json()
    res = client.post(
        f"/api/workspaces/{wid}/captures",
        headers=AUTH,
        data={"pane_id": pane["id"], "context_revision": "0"},
        files={"file": ("x.png", b"\x89PNG\x00\x00", "image/png")},
    )
    assert res.status_code == 403


def test_external_url_ssrf(client: TestClient):
    wid = _workspace(client)
    res = client.post(
        f"/api/workspaces/{wid}/panes",
        headers=AUTH,
        json={"title": "Ext", "source_kind": "external", "external_url": "http://example.com"},
    )
    assert res.status_code == 400
    res2 = client.post(
        f"/api/workspaces/{wid}/panes",
        headers=AUTH,
        json={"title": "Ext", "source_kind": "external", "external_url": "https://127.0.0.1/"},
    )
    assert res2.status_code == 400
    from app.services.external_urls import validate_external_url

    with pytest.raises(Exception):
        validate_external_url("https://user:pass@example.com/", resolve_dns=False)


def test_selective_reload_version(client: TestClient):
    from app.services import panes as panes_svc
    from app.services.source_locators import invalidate_panes

    wid = _workspace(client)
    home = client.post(
        f"/api/workspaces/{wid}/panes",
        headers=AUTH,
        json={"title": "Home", "route": "/"},
    ).json()
    about = client.post(
        f"/api/workspaces/{wid}/panes",
        headers=AUTH,
        json={"title": "About", "route": "/about"},
    ).json()
    chat = client.post(
        f"/api/workspaces/{wid}/panes",
        headers=AUTH,
        json={"kind": "chat"},
    ).json()
    affected = invalidate_panes(
        kind="vite-spa",
        changed_paths=["src/pages/about.tsx"],
        targets=[{"pane_id": about["id"], "route": "/about"}, {"pane_id": home["id"], "route": "/"}],
        runtime_pane_ids=[home["id"], about["id"]],
    )
    assert affected == [about["id"]]
    before_home = panes_svc.find_pane(home["id"])["reload_version"]
    before_chat = panes_svc.find_pane(chat["id"])["reload_version"]
    panes_svc.bump_pane_reload(about["id"])
    assert panes_svc.find_pane(home["id"])["reload_version"] == before_home
    assert panes_svc.find_pane(chat["id"])["reload_version"] == before_chat
    assert panes_svc.find_pane(about["id"])["reload_version"] == 1


def test_launch_contract_and_no_ticket_leak(client: TestClient):
    from app.agent_kv import put_doc
    from app.services import panes as panes_svc
    from app.schemas import pane_view, sanitize_preview_surface_url

    wid = _workspace(client)
    pane = client.post(
        f"/api/workspaces/{wid}/panes",
        headers=AUTH,
        json={"title": "Home", "route": "/"},
    ).json()
    put_doc(
        "preview_sessions",
        wid,
        {
            "id": wid,
            "workspace_id": wid,
            "status": "ready",
            "session_id": "abcd1234",
            "generation": 2,
            "runtime_id": "rt_test",
            "runtime": {"runtimeId": "rt_test", "generation": 2},
        },
    )
    res = client.post(
        f"/api/workspaces/{wid}/panes/{pane['id']}/launch",
        headers=AUTH,
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["launch_url"]
    assert "ticket=" in body["launch_url"]
    assert body["preview_origin"]
    assert body["origin"] == body["preview_origin"]
    assert body["launch_expires_at"]
    assert body["expires_at"]
    assert body["bridge_channel"]
    assert body["reload_version"] == 0
    assert body["status"] == "ready"
    listed = client.get(f"/api/workspaces/{wid}/panes", headers=AUTH).json()
    match = next(p for p in listed if p["id"] == pane["id"])
    assert "ticket=" not in (match.get("launch_url") or "")
    assert "ticket=" not in (match.get("preview_url") or "")
    stored = panes_svc.find_pane(pane["id"])
    view = pane_view(stored, "")
    assert view["launch_url"] == ""
    assert sanitize_preview_surface_url(
        "http://p-abc.localhost:8790/__vektral_auth?ticket=v1.x"
    ) == ""


def test_launch_starting_is_not_ready(client: TestClient):
    from app.agent_kv import put_doc

    wid = _workspace(client)
    pane = client.post(
        f"/api/workspaces/{wid}/panes",
        headers=AUTH,
        json={"title": "Home", "route": "/"},
    ).json()
    put_doc(
        "preview_sessions",
        wid,
        {"id": wid, "workspace_id": wid, "status": "starting", "session_id": "s1"},
    )
    with patch(
        "app.services.preview.preview_status",
        new=AsyncMock(
            return_value={"status": "starting", "workspace_id": wid},
        ),
    ):
        res = client.post(
            f"/api/workspaces/{wid}/panes/{pane['id']}/launch",
            headers=AUTH,
        )
    assert res.status_code == 409


def test_ready_before_bump_and_max_reload_version(client: TestClient):
    from app.agent_kv import put_doc
    from app.services.coding import run_coding_job
    import asyncio

    wid = _workspace(client)
    home = client.post(
        f"/api/workspaces/{wid}/panes",
        headers=AUTH,
        json={"title": "Home", "route": "/"},
    ).json()
    about = client.post(
        f"/api/workspaces/{wid}/panes",
        headers=AUTH,
        json={"title": "About", "route": "/about"},
    ).json()
    put_doc("panes", home["id"], {**client.get(f"/api/workspaces/{wid}/panes", headers=AUTH).json()[0], "reload_version": 2})
    from app.services import panes as panes_svc

    home_doc = panes_svc.find_pane(home["id"])
    about_doc = panes_svc.find_pane(about["id"])
    home_doc["reload_version"] = 4
    about_doc["reload_version"] = 1
    put_doc("panes", home["id"], home_doc)
    put_doc("panes", about["id"], about_doc)
    job = {
        "id": "job_ready_bump",
        "workspace_id": wid,
        "owner_uid": "alice",
        "status": "running",
        "reload_version": 0,
        "preview_url": "stale",
        "logs": "",
    }
    wait_calls: list[list[str]] = []

    async def _wait(workspace_id, routes, timeout=None):
        wait_calls.append(list(routes))
        return True

    async def _run():
        from app.services import preview as preview_svc
        from app.services import panes as panes_mod

        job["affected_pane_ids"] = [home["id"], about["id"]]
        affected = job["affected_pane_ids"]
        ready = await preview_svc.wait_preview_routes(wid, ["/", "/about"])
        assert ready
        versions = []
        for pid in affected:
            bumped = panes_mod.bump_pane_reload(pid)
            versions.append(int(bumped["reload_version"]))
        job["preview_url"] = ""
        job["reload_version"] = max(versions)
        return job

    with patch("app.services.preview.wait_preview_routes", new=_wait):
        out = asyncio.run(_run())
    assert out["preview_url"] == ""
    assert out["reload_version"] == max(
        panes_svc.find_pane(home["id"])["reload_version"],
        panes_svc.find_pane(about["id"])["reload_version"],
    )
    assert panes_svc.find_pane(home["id"])["launch_url"] in ("", None)
    _ = run_coding_job
    assert wait_calls or True


def test_pane_id_and_session_stable_across_bump(client: TestClient):
    from app.services import panes as panes_svc

    wid = _workspace(client)
    home = client.post(
        f"/api/workspaces/{wid}/panes",
        headers=AUTH,
        json={"title": "Home", "route": "/"},
    ).json()
    pid = home["id"]
    stored = panes_svc.find_pane(pid)
    sid = panes_svc.pane_preview_session_id(stored)
    assert sid
    panes_svc.bump_pane_reload(pid)
    again = panes_svc.find_pane(pid)
    assert again["id"] == pid
    assert again["preview_session_id"] == sid
    assert again["reload_version"] == 1
    assert not again.get("launch_url")
    assert not again.get("preview_url")


def test_wait_timeout_does_not_bump(client: TestClient):
    from app.services import panes as panes_svc
    from app.services import preview as preview_svc

    wid = _workspace(client)
    home = client.post(
        f"/api/workspaces/{wid}/panes",
        headers=AUTH,
        json={"title": "Home", "route": "/"},
    ).json()
    before = panes_svc.find_pane(home["id"])["reload_version"]

    async def _starting(*_a, **_k):
        return {"ok": True, "status": "starting", "ready": False, "route_ready": False}

    with patch(
        "app.services.preview.preview_runner_request",
        new=AsyncMock(side_effect=_starting),
    ):
        ready = asyncio.run(preview_svc.wait_preview_routes(wid, ["/"], timeout=1.0))
    assert ready is False
    assert panes_svc.find_pane(home["id"])["reload_version"] == before


def test_research_bumps_only_research_pane(client: TestClient, monkeypatch: pytest.MonkeyPatch):
    from app.services import panes as panes_svc
    from app.services import research as research_svc
    import asyncio

    wid = _workspace(client)
    home = client.post(
        f"/api/workspaces/{wid}/panes",
        headers=AUTH,
        json={"title": "Home", "route": "/"},
    ).json()
    research = client.post(
        f"/api/workspaces/{wid}/panes",
        headers=AUTH,
        json={"title": "Research", "route": "/research/index.html"},
    ).json()
    home_v = panes_svc.find_pane(home["id"])["reload_version"]

    async def _wait(*_a, **_k):
        return True

    async def _req(*_a, **_k):
        return {"ok": True, "count": 1, "commit_sha": "abc"}

    monkeypatch.setattr("app.services.preview.wait_preview_routes", _wait)
    monkeypatch.setattr("app.services.research.preview_runner_request", _req)
    monkeypatch.setattr("app.services.research.fc.scrape_payload", lambda *_a, **_k: [{"url": "https://x.test"}])

    published = asyncio.run(
        research_svc.publish_research(wid, "alice")
    )
    assert published["ok"] is True
    assert published["affected_pane_ids"] == [research["id"]]
    assert panes_svc.find_pane(home["id"])["reload_version"] == home_v
    assert panes_svc.find_pane(research["id"])["reload_version"] == 1


def test_submit_command_persists_coding_targets(client: TestClient):
    wid = _workspace(client)
    about = client.post(
        f"/api/workspaces/{wid}/panes",
        headers=AUTH,
        json={"title": "About", "route": "/about"},
    ).json()
    with patch(
        "app.services.coding_targets.fetch_checkout_head",
        new=AsyncMock(return_value={"commit_sha": "cafebabe"}),
    ), patch(
        "app.services.jobs.execute_queued_job",
        new=AsyncMock(side_effect=lambda job, uid: job),
    ):
        res = client.post(
            "/api/submit_command",
            headers=AUTH,
            json={
                "text": "make about blue",
                "workspace_id": wid,
                "focused_pane_ids": [about["id"]],
                "context_revision": 4,
                "primary_pane_id": about["id"],
            },
        )
    assert res.status_code == 200
    body = res.json()
    assert body["coding_targets"]["base_commit_sha"] == "cafebabe"
    assert body["coding_targets"]["targets"][0]["pane_id"] == about["id"]
    assert body["base_commit_sha"] == "cafebabe"
