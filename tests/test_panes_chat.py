"""Chat panes, message history, and coding repo-context tests."""

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


AUTH_A = {"Authorization": "Bearer dev:alice"}


def _workspace(client: TestClient) -> str:
    res = client.post(
        "/api/workspaces",
        headers=AUTH_A,
        json={"name": "Chat WS", "description": "panes"},
    )
    assert res.status_code in (200, 201)
    return res.json()["id"]


def test_create_chat_pane_and_list_kinds(client: TestClient):
    wid = _workspace(client)
    home = client.post(
        f"/api/workspaces/{wid}/panes",
        headers=AUTH_A,
        json={"title": "Home", "route": "/"},
    )
    assert home.status_code == 200
    assert home.json()["kind"] == "preview"
    assert home.json()["route"] == "/"

    chat = client.post(
        f"/api/workspaces/{wid}/panes",
        headers=AUTH_A,
        json={"title": "New chat", "kind": "chat"},
    )
    assert chat.status_code == 200
    data = chat.json()
    assert data["kind"] == "chat"
    assert data["route"].startswith("/chat/")
    assert data["preview_url"] == ""

    listed = client.get(f"/api/workspaces/{wid}/panes", headers=AUTH_A)
    kinds = {p["kind"] for p in listed.json()}
    assert kinds == {"preview", "chat"}

    chats_only = client.get(
        f"/api/workspaces/{wid}/panes",
        headers=AUTH_A,
        params={"kind": "chat"},
    )
    assert all(p["kind"] == "chat" for p in chats_only.json())

    sess = client.get("/api/session", headers=AUTH_A)
    assert sess.status_code == 200
    slice_ = sess.json()["panes"][wid]
    assert slice_["active_chat_pane_id"] == data["id"]
    assert any(i["kind"] == "chat" for i in slice_["items"])


def test_messages_404_on_preview_pane(client: TestClient):
    wid = _workspace(client)
    home = client.post(
        f"/api/workspaces/{wid}/panes",
        headers=AUTH_A,
        json={"title": "Home", "route": "/"},
    ).json()
    res = client.get(
        f"/api/workspaces/{wid}/panes/{home['id']}/messages",
        headers=AUTH_A,
    )
    assert res.status_code == 404

    activate = client.post(
        f"/api/workspaces/{wid}/panes/{home['id']}/activate",
        headers=AUTH_A,
    )
    assert activate.status_code == 400


def test_omit_pane_id_continues_active_chat(client: TestClient):
    wid = _workspace(client)
    first = client.post(
        f"/api/workspaces/{wid}/panes",
        headers=AUTH_A,
        json={"kind": "chat", "title": "Chat"},
    ).json()

    res = client.post(
        "/api/vocalbridge/query",
        headers=AUTH_A,
        json={"text": "hello there", "workspace_id": wid},
    )
    assert res.status_code == 200

    msgs = client.get(
        f"/api/workspaces/{wid}/panes/{first['id']}/messages",
        headers=AUTH_A,
    )
    assert msgs.status_code == 200
    texts = [m["text"] for m in msgs.json()]
    assert "hello there" in texts
    assert any(m["role"] == "assistant" for m in msgs.json())


def test_new_conversation_creates_fresh_chat_pane(client: TestClient):
    wid = _workspace(client)
    first = client.post(
        f"/api/workspaces/{wid}/panes",
        headers=AUTH_A,
        json={"kind": "chat", "title": "Chat"},
    ).json()

    res = client.post(
        "/api/submit_command",
        headers=AUTH_A,
        json={
            "text": "start over",
            "workspace_id": wid,
            "new_conversation": True,
        },
    )
    assert res.status_code == 200

    chats = client.get(
        f"/api/workspaces/{wid}/panes",
        headers=AUTH_A,
        params={"kind": "chat"},
    ).json()
    assert len(chats) >= 2
    ids = {c["id"] for c in chats}
    assert first["id"] in ids
    new_ids = ids - {first["id"]}
    assert new_ids

    sess = client.get("/api/session", headers=AUTH_A).json()
    assert sess["panes"][wid]["active_chat_pane_id"] in new_ids

    old_msgs = client.get(
        f"/api/workspaces/{wid}/panes/{first['id']}/messages",
        headers=AUTH_A,
    ).json()
    assert all("start over" not in m["text"] for m in old_msgs)


def test_bump_skips_chat_panes(client: TestClient):
    from app.services import panes as panes_svc

    wid = _workspace(client)
    home = client.post(
        f"/api/workspaces/{wid}/panes",
        headers=AUTH_A,
        json={"title": "Home", "route": "/"},
    ).json()
    chat = client.post(
        f"/api/workspaces/{wid}/panes",
        headers=AUTH_A,
        json={"kind": "chat", "title": "Chat"},
    ).json()

    bumped = panes_svc.bump_workspace_panes(wid)
    assert all(p["kind"] != "chat" for p in bumped)
    assert any(p["id"] == home["id"] for p in bumped)

    chat_after = panes_svc.bump_pane_reload(chat["id"])
    assert chat_after is not None
    assert chat_after["reload_version"] == 0


def test_gather_repo_context_reads_collab_files():
    import asyncio

    from app.services import coding

    async def _run() -> tuple[str, list[str]]:
        with patch.object(
            coding,
            "git_tree",
            new=AsyncMock(
                return_value={
                    "ok": True,
                    "entries": [
                        {
                            "name": "App.tsx",
                            "path": "src/App.tsx",
                            "type": "file",
                            "size": 12,
                        }
                    ],
                }
            ),
        ), patch.object(
            coding,
            "git_file",
            new=AsyncMock(
                return_value={"ok": True, "path": "src/App.tsx", "content": "export default function App() {}"}
            ),
        ):
            return await coding.gather_repo_context("ws_1", "/", "change App.tsx header")

    blob, paths = asyncio.run(_run())
    assert "src/App.tsx" in paths
    assert "export default function App" in blob
    assert blob.startswith("Repo files:")


def test_history_for_llm_excludes_current_utterance(client: TestClient):
    from app.services import panes as panes_svc

    wid = _workspace(client)
    chat = client.post(
        f"/api/workspaces/{wid}/panes",
        headers=AUTH_A,
        json={"kind": "chat"},
    ).json()
    panes_svc.append_message(chat["id"], "alice", "user", "first")
    panes_svc.append_message(chat["id"], "alice", "assistant", "ok")
    panes_svc.append_message(chat["id"], "alice", "user", "follow up")
    hist = panes_svc.history_for_llm(chat["id"], exclude_text="follow up")
    assert hist[-1]["content"] == "ok"
    assert all(h["content"] != "follow up" for h in hist)
