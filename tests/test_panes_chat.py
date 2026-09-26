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


AUTH_A = {"Authorization": "Bearer dev:alice"}


def _workspace(client: TestClient) -> str:
    res = client.post(
        "/api/workspaces",
        headers=AUTH_A,
        json={"name": "Chat WS", "description": "panes"},
    )
    assert res.status_code in (200, 201)
    return res.json()["id"]


def test_blank_pane_converts_in_place(client: TestClient):
    wid = _workspace(client)
    created = client.post(
        f"/api/workspaces/{wid}/panes",
        headers=AUTH_A,
        json={"title": "Blank", "kind": "blank"},
    )
    assert created.status_code == 200
    blank = created.json()
    assert blank["kind"] == "blank"
    assert blank["route"] == ""
    assert blank["preview_url"] == ""
    assert blank["source_kind"] == ""
    pane_id = blank["id"]

    page = client.put(
        f"/api/workspaces/{wid}/panes/{pane_id}",
        headers=AUTH_A,
        json={"kind": "preview", "route": "/shop", "title": "Shop"},
    )
    assert page.status_code == 200
    assert page.json()["id"] == pane_id
    assert page.json()["kind"] == "preview"
    assert page.json()["source_kind"] == "workspace-preview"
    assert page.json()["route"] == "/shop"
    assert page.json()["title"] == "Shop"

    chat = client.put(
        f"/api/workspaces/{wid}/panes/{pane_id}",
        headers=AUTH_A,
        json={"kind": "chat", "title": "Notes"},
    )
    assert chat.status_code == 200
    assert chat.json()["id"] == pane_id
    assert chat.json()["kind"] == "chat"
    assert chat.json()["route"] == f"/chat/{pane_id}"
    assert chat.json()["preview_url"] == ""

    emptied = client.put(
        f"/api/workspaces/{wid}/panes/{pane_id}",
        headers=AUTH_A,
        json={"kind": "blank"},
    )
    assert emptied.status_code == 200
    assert emptied.json()["id"] == pane_id
    assert emptied.json()["kind"] == "blank"
    assert emptied.json()["route"] == ""


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


def test_voice_creates_and_reuses_preview_screens(client: TestClient):
    from app.services.panes import is_screen_command, parse_screen_targets

    assert is_screen_command("create a new screen for about")
    assert is_screen_command("open the checkout page")
    assert is_screen_command("create screens for about and pricing")
    assert is_screen_command("open another screen")
    assert is_screen_command("place a new screen")
    assert not is_screen_command("open that screen")
    assert not is_screen_command("make the hero green")
    assert not is_screen_command("create an issue for login")
    assert not is_screen_command("walk over to that screen")
    targets = parse_screen_targets("create screens for about and checkout")
    assert [t["route"] for t in targets] == ["/about", "/checkout"]
    another = parse_screen_targets("open another screen")
    assert another == [{"title": "Screen", "route": "", "force_new": "1"}]
    assert parse_screen_targets("open another screen")[0]["route"] != "/another"

    wid = _workspace(client)
    with patch("app.services.jobs.run_coding_job") as coding:
        first = client.post(
            "/api/vocalbridge/query",
            headers=AUTH_A,
            json={"text": "create a new screen for about", "workspace_id": wid},
        )
        again = client.post(
            "/api/vocalbridge/query",
            headers=AUTH_A,
            json={"text": "open the about page", "workspace_id": wid},
        )
        duo = client.post(
            "/api/vocalbridge/query",
            headers=AUTH_A,
            json={"text": "create screens for pricing and contact", "workspace_id": wid},
        )
    assert first.status_code == 200
    body = first.json()
    assert body["status"] == "ready"
    plan = body.get("result_json") or ""
    import json

    payload = json.loads(plan) if isinstance(plan, str) else plan
    assert payload["action"] == "open_screen"
    assert payload["panes"][0]["route"] == "/about"
    assert payload["panes"][0]["created"] is True
    again_raw = again.json()["result_json"]
    second = json.loads(again_raw) if isinstance(again_raw, str) else again_raw
    assert second["panes"][0]["id"] == payload["panes"][0]["id"]
    assert second["panes"][0]["created"] is False
    duo_raw = duo.json()["result_json"]
    both = json.loads(duo_raw) if isinstance(duo_raw, str) else duo_raw
    assert {p["route"] for p in both["panes"]} == {"/pricing", "/contact"}
    fresh = client.post(
        "/api/vocalbridge/query",
        headers=AUTH_A,
        json={
            "text": "open another screen",
            "workspace_id": wid,
            "turn_id": "turn-another-screen",
            "is_final": True,
        },
    )
    assert fresh.status_code == 200, fresh.text
    fresh_raw = fresh.json()["result_json"]
    opened = json.loads(fresh_raw) if isinstance(fresh_raw, str) else fresh_raw
    assert opened["action"] == "open_screen"
    assert opened["panes"][0]["created"] is True
    assert opened["panes"][0]["id"]
    assert opened["panes"][0]["route"] != "/another"
    coding.assert_not_called()
    listed = client.get(f"/api/workspaces/{wid}/panes", headers=AUTH_A).json()
    preview_routes = {p["route"] for p in listed if p["kind"] == "preview"}
    assert {"/about", "/pricing", "/contact"} <= preview_routes
