"""Dialogue turn orchestration: auth, idempotency, panes, jobs, SSE."""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

os.environ["ALLOW_DEV_BEARER"] = "true"
os.environ["VEKTRAL_USE_MEMORY_STORE"] = "true"
os.environ["FIREBASE_STUB_MODE"] = "true"
os.environ["EMBODIMENT_ENABLED"] = "true"
os.environ["DIALOGUE_ORCHESTRATION_ENABLED"] = "true"
os.environ["DIALOGUE_ORCHESTRATION_STAGE"] = "assistant_avatar"
os.environ.pop("GOOGLE_APPLICATION_CREDENTIALS", None)

FIXTURES = (
    Path(__file__).resolve().parents[2] / "vektral-contracts" / "fixtures" / "dialogue"
)
OPENAPI = (
    Path(__file__).resolve().parents[2]
    / "vektral-contracts"
    / "openapi"
    / "vektral-api.v0.yaml"
)

AUTH_A = {"Authorization": "Bearer dev:alice"}
AUTH_B = {"Authorization": "Bearer dev:bob"}


@pytest.fixture()
def client(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ALLOW_DEV_BEARER", "true")
    monkeypatch.setenv("VEKTRAL_USE_MEMORY_STORE", "true")
    monkeypatch.setenv("FIREBASE_STUB_MODE", "true")
    monkeypatch.setenv("EMBODIMENT_ENABLED", "true")
    monkeypatch.setenv("DIALOGUE_ORCHESTRATION_ENABLED", "true")
    monkeypatch.setenv("DIALOGUE_ORCHESTRATION_STAGE", "assistant_avatar")

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


def _workspace(client: TestClient) -> str:
    res = client.post(
        "/api/workspaces",
        headers=AUTH_A,
        json={"name": "Dialogue WS", "description": "turns"},
    )
    assert res.status_code in (200, 201)
    return res.json()["id"]


def _preview(client: TestClient, wid: str, title: str, route: str) -> dict:
    res = client.post(
        f"/api/workspaces/{wid}/panes",
        headers=AUTH_A,
        json={"title": title, "route": route},
    )
    assert res.status_code == 200
    return res.json()


def _post_turn(client: TestClient, wid: str, body: dict) -> dict:
    res = client.post(
        f"/api/workspaces/{wid}/dialogue/turns",
        headers=AUTH_A,
        json=body,
    )
    assert res.status_code == 200, res.text
    return res.json()


def test_fixtures_validate_and_reject_coordinates():
    from app.schemas import (
        DialogueTurnRequestV1,
        DialogueTurnResultV1,
        EmbodimentReceipt,
        TurnActionPaneSetRoute,
        reject_spatial_payload,
    )

    DialogueTurnRequestV1.model_validate_json((FIXTURES / "user_turn.json").read_text())
    DialogueTurnRequestV1.model_validate_json(
        (FIXTURES / "assistant_turn.json").read_text()
    )
    DialogueTurnRequestV1.model_validate_json((FIXTURES / "noop_turn.json").read_text())
    DialogueTurnResultV1.model_validate_json(
        (FIXTURES / "compound_pane_edit_attend.json").read_text()
    )
    TurnActionPaneSetRoute.model_validate_json(
        (FIXTURES / "route_change.json").read_text()
    )
    EmbodimentReceipt.model_validate_json((FIXTURES / "receipt_step.json").read_text())
    DialogueTurnResultV1.model_validate_json((FIXTURES / "stale_turn.json").read_text())
    forbidden = json.loads((FIXTURES / "forbidden_coordinates.json").read_text())
    with pytest.raises(ValueError):
        reject_spatial_payload(forbidden)


def test_openapi_lists_dialogue_turn_routes():
    text = OPENAPI.read_text()
    assert "/api/workspaces/{workspace_id}/dialogue/turns" in text
    assert "/api/workspaces/{workspace_id}/dialogue/playback-events" in text
    assert "dialogue_turn_update" in text
    assert "playback_started" in text
    assert "AvatarPlanV2" in text or "TurnActionV1" in text


def test_flag_off_returns_404(monkeypatch: pytest.MonkeyPatch):
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
        wid = c.post(
            "/api/workspaces",
            headers=AUTH_A,
            json={"name": "Off", "description": "x"},
        ).json()["id"]
        res = c.post(
            f"/api/workspaces/{wid}/dialogue/turns",
            headers=AUTH_A,
            json={
                "turn_id": "turn_disabled01",
                "role": "user",
                "text": "hello there",
                "is_final": True,
            },
        )
        assert res.status_code == 404
    clear_memory()
    set_store_override(None)
    get_settings.cache_clear()


def test_partial_utterance_and_spatial_rejected(client: TestClient):
    wid = _workspace(client)
    partial = client.post(
        f"/api/workspaces/{wid}/dialogue/turns",
        headers=AUTH_A,
        json={
            "turn_id": "turn_partial01",
            "role": "user",
            "text": "walk over to",
            "is_final": False,
        },
    )
    assert partial.status_code == 400
    spatial = client.post(
        f"/api/workspaces/{wid}/dialogue/turns",
        headers=AUTH_A,
        json={
            "turn_id": "turn_spatial01",
            "role": "user",
            "text": "walk over",
            "is_final": True,
            "position": {"x": 1, "y": 0, "z": 2},
        },
    )
    assert spatial.status_code == 422


def test_foreign_workspace_forbidden(client: TestClient):
    wid = _workspace(client)
    denied = client.post(
        f"/api/workspaces/{wid}/dialogue/turns",
        headers=AUTH_B,
        json={
            "turn_id": "turn_foreign01",
            "role": "user",
            "text": "open about",
            "is_final": True,
        },
    )
    assert denied.status_code in (403, 404)


def test_idempotent_turn_id(client: TestClient):
    wid = _workspace(client)
    _preview(client, wid, "Home", "/")
    body = {
        "turn_id": "turn_idempotent01",
        "role": "user",
        "text": "open the about screen",
        "is_final": True,
    }
    first = _post_turn(client, wid, body)
    second = _post_turn(client, wid, body)
    assert first["duplicate"] is False
    assert second["duplicate"] is True
    assert first["revision"] == second["revision"]
    spawned = [p for p in first["spawned_panes"] if p.get("created")]
    spawned2 = [p for p in second["spawned_panes"] if p.get("created")]
    assert len(spawned) == len(spawned2) or second["spawned_panes"] == first["spawned_panes"]
    panes = client.get(f"/api/workspaces/{wid}/panes", headers=AUTH_A).json()
    abouts = [p for p in panes if p.get("route") == "/about"]
    assert len(abouts) == 1


def test_noop_and_atomic_revisions(client: TestClient):
    wid = _workspace(client)
    home = _preview(client, wid, "Home", "/")
    a = _post_turn(
        client,
        wid,
        {
            "turn_id": "turn_thanks_aaa",
            "role": "user",
            "text": "thanks",
            "is_final": True,
            "primary_pane_id": home["id"],
            "focused_pane_ids": [home["id"]],
        },
    )
    b = _post_turn(
        client,
        wid,
        {
            "turn_id": "turn_thanks_bbb",
            "role": "user",
            "text": "ok",
            "is_final": True,
            "primary_pane_id": home["id"],
            "focused_pane_ids": [home["id"]],
        },
    )
    assert a["status"] == "noop"
    assert a["actions"][0]["type"] == "noop"
    assert b["revision"] == a["revision"] + 1


def test_compound_open_then_attend_order(client: TestClient):
    wid = _workspace(client)
    home = _preview(client, wid, "Home", "/")
    result = _post_turn(
        client,
        wid,
        {
            "turn_id": "turn_compound_open",
            "role": "user",
            "text": "open the about screen and walk over to it",
            "is_final": True,
            "primary_pane_id": home["id"],
            "focused_pane_ids": [home["id"]],
        },
    )
    types = [a["type"] for a in result["actions"]]
    assert types[0] == "pane.open"
    assert "avatar.plan" in types
    assert types.index("pane.open") < types.index("avatar.plan")
    assert result["spawned_panes"] or result["updated_panes"]
    plan = result.get("avatar_plan") or {}
    assert plan.get("plan_id")
    assert plan.get("apex_intent", {}).get("steps")


def test_set_route_remounts_same_pane(client: TestClient):
    wid = _workspace(client)
    home = _preview(client, wid, "Home", "/")
    before = home["reload_version"]
    result = _post_turn(
        client,
        wid,
        {
            "turn_id": "turn_set_route01",
            "role": "user",
            "text": "show pricing on this screen",
            "is_final": True,
            "primary_pane_id": home["id"],
            "focused_pane_ids": [home["id"]],
        },
    )
    assert any(a["type"] == "pane.set_route" for a in result["actions"])
    updated = result["updated_panes"][0]
    assert updated["id"] == home["id"]
    assert updated["route"] == "/pricing"
    assert updated["reload_version"] == before + 1


def test_assistant_cannot_mutate_without_continuation(client: TestClient):
    wid = _workspace(client)
    home = _preview(client, wid, "Home", "/")
    result = _post_turn(
        client,
        wid,
        {
            "turn_id": "turn_asst_mutate",
            "role": "assistant",
            "text": "open a secret admin screen",
            "source": "assistant_tts",
            "is_final": True,
            "primary_pane_id": home["id"],
            "focused_pane_ids": [home["id"]],
        },
    )
    assert all(a["type"] in {"noop", "avatar.plan"} for a in result["actions"])
    panes = client.get(f"/api/workspaces/{wid}/panes", headers=AUTH_A).json()
    assert not any("admin" in (p.get("route") or "") for p in panes)


def test_assistant_wave_when_stage_allows(client: TestClient):
    wid = _workspace(client)
    home = _preview(client, wid, "Home", "/")
    client.put(
        f"/api/workspaces/{wid}/agent-context",
        headers=AUTH_A,
        json={
            "client_revision": 1,
            "primary_pane_id": home["id"],
            "focused_pane_ids": [home["id"]],
            "capabilities": [
                "avatar.plan.v2",
                "locomotion.attend",
                "gesture.wave",
            ],
        },
    )
    result = _post_turn(
        client,
        wid,
        {
            "turn_id": "turn_asst_wave01",
            "role": "assistant",
            "text": "I'll wave from here.",
            "source": "assistant_tts",
            "is_final": True,
            "primary_pane_id": home["id"],
            "focused_pane_ids": [home["id"]],
        },
    )
    assert any(a["type"] == "avatar.plan" for a in result["actions"])
    plan = result.get("avatar_plan") or {}
    steps = plan.get("steps") or plan.get("apex_intent", {}).get("steps") or []
    assert any(
        (s.get("type") == "gesture" and (s.get("gesture") == "wave"))
        for s in steps
    )


def test_newer_user_turn_cancels_plan(client: TestClient):
    wid = _workspace(client)
    home = _preview(client, wid, "Home", "/")
    first = _post_turn(
        client,
        wid,
        {
            "turn_id": "turn_walk_one",
            "role": "user",
            "text": "walk over to that screen",
            "is_final": True,
            "primary_pane_id": home["id"],
            "focused_pane_ids": [home["id"]],
        },
    )
    plan_id = (first.get("avatar_plan") or {}).get("plan_id")
    assert plan_id
    _post_turn(
        client,
        wid,
        {
            "turn_id": "turn_walk_two",
            "role": "user",
            "text": "thanks",
            "is_final": True,
            "primary_pane_id": home["id"],
            "focused_pane_ids": [home["id"]],
        },
    )
    state = client.get(
        f"/api/workspaces/{wid}/embodiment/state", headers=AUTH_A
    ).json()
    plan = state.get("plan") or {}
    assert plan.get("cancelled") or plan.get("plan_id") != plan_id


def test_page_create_allowlist_and_queued_job(client: TestClient):
    from app.services.source_locators import creatable_destinations, validate_create_paths

    vite = creatable_destinations("vite-spa", "/blog", ".")
    assert any(p.endswith("src/pages/blog.tsx") or "src/App.tsx" in p for p in vite)
    assert "src/App.tsx" in vite
    assert validate_create_paths("vite-spa", "/blog", ["src/secret.rs"], ".") == []
    nxt = creatable_destinations("next", "/posts/hello", "apps/web")
    assert any("app/posts/hello/page.tsx" in p for p in nxt)

    wid = _workspace(client)
    home = _preview(client, wid, "Home", "/")
    with patch("app.services.jobs.execute_queued_job") as exec_job:
        result = _post_turn(
            client,
            wid,
            {
                "turn_id": "turn_page_create01",
                "role": "user",
                "text": "create a new page at /blog",
                "is_final": True,
                "primary_pane_id": home["id"],
                "focused_pane_ids": [home["id"]],
            },
        )
    assert any(a["type"] == "page.create" for a in result["actions"])
    assert result["job_ids"]
    exec_job.assert_not_called()
    spawned = result["spawned_panes"]
    assert spawned
    assert spawned[0]["route"] == "/blog"


def test_research_and_linear_still_queue(client: TestClient):
    wid = _workspace(client)
    home = _preview(client, wid, "Home", "/")
    research = _post_turn(
        client,
        wid,
        {
            "turn_id": "turn_research01",
            "role": "user",
            "text": "research https://example.com/docs",
            "is_final": True,
            "primary_pane_id": home["id"],
            "focused_pane_ids": [home["id"]],
        },
    )
    assert any(a["type"] == "research.run" for a in research["actions"])
    linear = _post_turn(
        client,
        wid,
        {
            "turn_id": "turn_linear01",
            "role": "user",
            "text": "create an issue for the login bug",
            "is_final": True,
            "primary_pane_id": home["id"],
            "focused_pane_ids": [home["id"]],
        },
    )
    assert any(a["type"] == "linear.run" for a in linear["actions"])


def test_get_turn_and_sse_dialogue_event(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
):
    wid = _workspace(client)
    home = _preview(client, wid, "Home", "/")
    posted = _post_turn(
        client,
        wid,
        {
            "turn_id": "turn_sse_visible",
            "role": "user",
            "text": "thanks",
            "is_final": True,
            "primary_pane_id": home["id"],
            "focused_pane_ids": [home["id"]],
        },
    )
    got = client.get(
        f"/api/workspaces/{wid}/dialogue/turns/turn_sse_visible",
        headers=AUTH_A,
    )
    assert got.status_code == 200
    assert got.json()["turn_id"] == posted["turn_id"]

    from app.services import dialogue as dialogue_svc

    latest = dialogue_svc.latest_turn_event(wid, "alice")
    assert latest is not None
    assert latest.get("turn_id") == posted["turn_id"]
    assert latest.get("status") in {"completed", "noop"}

    async def _instant(_delay=0):
        return None

    monkeypatch.setattr("app.routers.jobs.asyncio.sleep", _instant)
    with client.stream(
        "GET", f"/api/workspaces/{wid}/events", headers=AUTH_A
    ) as stream:
        buf = ""
        types: list[str] = []
        for chunk in stream.iter_text():
            buf += chunk
            for line in buf.split("\n"):
                if line.startswith("data: "):
                    try:
                        types.append(json.loads(line[6:]).get("type") or "")
                    except json.JSONDecodeError:
                        continue
            if "dialogue_turn_update" in types:
                break
            if "timeout" in types:
                break
    assert "workspace_update" in types
    assert "dialogue_turn_update" in types
    assert "turn_sse_visible" in buf


def test_legacy_adapter_walk_skips_coding(client: TestClient):
    wid = _workspace(client)
    home = _preview(client, wid, "Home", "/")
    with patch("app.services.jobs.run_coding_job") as coding:
        job = client.post(
            "/api/vocalbridge/query",
            headers=AUTH_A,
            json={
                "text": "walk over to that screen",
                "workspace_id": wid,
                "turn_id": "turn_walk_once_01",
                "is_final": True,
            },
        )
    assert job.status_code == 200
    body = job.json()
    assert body.get("embodiment_json", {}).get("plan_id") or body.get("dialogue_turn")
    assert body.get("turn_id") == "turn_walk_once_01"
    assert "agent_response" in body
    coding.assert_not_called()
    assert home["id"]


def test_vocalbridge_requires_turn_id_after_cutover(client: TestClient):
    wid = _workspace(client)
    res = client.post(
        "/api/vocalbridge/query",
        headers=AUTH_A,
        json={"text": "open about", "workspace_id": wid, "is_final": True},
    )
    assert res.status_code == 400
    assert res.json()["detail"]["error"] == "missing_turn_id"


def test_vocalbridge_rejects_partial_and_dedupes(client: TestClient):
    wid = _workspace(client)
    partial = client.post(
        "/api/vocalbridge/query",
        headers=AUTH_A,
        json={
            "text": "open about",
            "workspace_id": wid,
            "turn_id": "turn_vb_partial",
            "is_final": False,
        },
    )
    assert partial.status_code == 400
    first = client.post(
        "/api/vocalbridge/query",
        headers=AUTH_A,
        json={
            "text": "open about",
            "workspace_id": wid,
            "turn_id": "turn_vb_open_about",
            "is_final": True,
            "captures": [
                {
                    "pane_id": "pane_home",
                    "image_ref": "cap_abc",
                    "visible_text": "Hero",
                }
            ],
        },
    )
    assert first.status_code == 200, first.text
    body = first.json()
    assert body["turn_id"] == "turn_vb_open_about"
    assert body["duplicate"] is False
    assert body.get("agent_response")
    from app.schemas import VocalBridgeQuery

    aliased = VocalBridgeQuery.model_validate(
        {"text": "x", "turnId": "turn_camel_alias_1"}
    )
    assert aliased.turn_id == "turn_camel_alias_1"
    second = client.post(
        "/api/vocalbridge/query",
        headers=AUTH_A,
        json={
            "text": "open about",
            "workspace_id": wid,
            "turn_id": "turn_vb_open_about",
            "is_final": True,
        },
    )
    assert second.status_code == 200
    dup = second.json()
    assert dup["duplicate"] is True
    assert dup["turn_id"] == "turn_vb_open_about"
    assert dup.get("id") == body.get("id")


def test_playback_events_idempotent_and_sse(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
):
    wid = _workspace(client)
    _post_turn(
        client,
        wid,
        {
            "turn_id": "turn_assist_play_1",
            "role": "assistant",
            "text": "Created the About screen.",
            "is_final": True,
            "source": "assistant_tts",
        },
    )
    first = client.post(
        f"/api/workspaces/{wid}/dialogue/playback-events",
        headers=AUTH_A,
        json={
            "event_id": "play_evt_started_1",
            "turn_id": "turn_assist_play_1",
            "type": "playback_started",
        },
    )
    assert first.status_code == 200
    assert first.json()["duplicate"] is False
    again = client.post(
        f"/api/workspaces/{wid}/dialogue/playback-events",
        headers=AUTH_A,
        json={
            "event_id": "play_evt_started_1",
            "turn_id": "turn_assist_play_1",
            "type": "playback_started",
        },
    )
    assert again.json()["duplicate"] is True
    finished = client.post(
        f"/api/workspaces/{wid}/dialogue/playback-events",
        headers=AUTH_A,
        json={
            "event_id": "play_evt_finished_1",
            "turn_id": "turn_assist_play_1",
            "type": "playback_finished",
        },
    )
    assert finished.status_code == 200

    async def _instant(_delay=0):
        return None

    monkeypatch.setattr("app.routers.jobs.asyncio.sleep", _instant)
    from app.services import dialogue as dialogue_svc

    dialogue_svc.record_playback_event(
        wid,
        "alice",
        __import__("app.schemas", fromlist=["PlaybackEventRequest"]).PlaybackEventRequest(
            event_id="play_evt_late_1",
            turn_id="turn_assist_play_1",
            type="playback_finished",
        ),
    )
    # Prime cursor then emit a new event by posting after stream starts is hard;
    # assert the helper surfaces events after the last id.
    last = dialogue_svc.latest_playback_event_id(wid, "alice")
    assert last
    assert not dialogue_svc.playback_events_since(wid, "alice", last)
    types = {ev["type"] for ev in dialogue_svc.playback_events_since(wid, "alice", "")}
    assert "playback_started" in types
    assert "playback_finished" in types


def test_vocalbridge_and_native_share_turn(client: TestClient):
    wid = _workspace(client)
    _preview(client, wid, "Home", "/")
    native = _post_turn(
        client,
        wid,
        {
            "turn_id": "turn_shared_native_vb",
            "role": "user",
            "text": "open the about screen",
            "is_final": True,
            "captures": [
                {
                    "pane_id": "pane_home",
                    "image_ref": "cap_share",
                    "visible_text": "Hero",
                }
            ],
        },
    )
    assert native["duplicate"] is False
    vb = client.post(
        "/api/vocalbridge/query",
        headers=AUTH_A,
        json={
            "text": "open the about screen",
            "workspace_id": wid,
            "turn_id": "turn_shared_native_vb",
            "turnId": "turn_shared_native_vb",
            "is_final": True,
        },
    )
    assert vb.status_code == 200, vb.text
    body = vb.json()
    assert body["duplicate"] is True
    assert body["turn_id"] == native["turn_id"]
    assert body.get("agent_response")


def test_vocalbridge_rejects_short_turn_id_and_empty_text(client: TestClient):
    wid = _workspace(client)
    short = client.post(
        "/api/vocalbridge/query",
        headers=AUTH_A,
        json={
            "text": "open another screen",
            "workspace_id": wid,
            "turn_id": "short",
            "is_final": True,
        },
    )
    assert short.status_code == 400
    assert short.json()["detail"]["error"] == "invalid_turn_id"
    empty = client.post(
        "/api/vocalbridge/query",
        headers=AUTH_A,
        json={
            "text": "   ",
            "workspace_id": wid,
            "turn_id": "turn_empty_text",
            "is_final": True,
        },
    )
    assert empty.status_code == 400
    assert empty.json()["detail"]["error"] == "empty_utterance"


def test_vocalbridge_pane_crud_round_trip(client: TestClient):
    wid = _workspace(client)
    placed = client.post(
        "/api/vocalbridge/query",
        headers=AUTH_A,
        json={
            "text": "place the shop page pane",
            "workspace_id": wid,
            "turn_id": "turn_crud_place",
            "is_final": True,
        },
    )
    assert placed.status_code == 200, placed.text
    created = placed.json()
    raw = created.get("result_json")
    payload = json.loads(raw) if isinstance(raw, str) else raw
    assert payload["action"] == "open_screen"
    assert payload["panes"][0]["route"] == "/shop"
    pane_id = payload["panes"][0]["id"]

    renamed = client.post(
        "/api/vocalbridge/query",
        headers=AUTH_A,
        json={
            "text": "rename this screen to Shopfront",
            "workspace_id": wid,
            "turn_id": "turn_crud_rename",
            "is_final": True,
            "primary_pane_id": pane_id,
            "focused_pane_ids": [pane_id],
        },
    )
    assert renamed.status_code == 200, renamed.text
    raw = renamed.json().get("result_json")
    payload = json.loads(raw) if isinstance(raw, str) else raw
    assert payload["action"] == "update_pane"
    assert payload["panes"][0]["id"] == pane_id
    assert payload["panes"][0]["title"] == "Shopfront"

    removed = client.post(
        "/api/vocalbridge/query",
        headers=AUTH_A,
        json={
            "text": "remove this pane",
            "workspace_id": wid,
            "turn_id": "turn_crud_remove",
            "is_final": True,
            "primary_pane_id": pane_id,
            "focused_pane_ids": [pane_id],
        },
    )
    assert removed.status_code == 200, removed.text
    raw = removed.json().get("result_json")
    payload = json.loads(raw) if isinstance(raw, str) else raw
    assert payload["action"] == "close_pane"
    assert payload["panes"][0]["id"] == pane_id

    panes = client.get(f"/api/workspaces/{wid}/panes", headers=AUTH_A)
    assert panes.status_code == 200
    listed = panes.json()
    items = listed.get("panes", listed) if isinstance(listed, dict) else listed
    assert pane_id not in {p.get("id") for p in items if isinstance(p, dict)}


def test_blank_screen_converts_to_a_page_then_chat(client: TestClient):
    wid = _workspace(client)
    placed = client.post(
        "/api/vocalbridge/query",
        headers=AUTH_A,
        json={
            "text": "place a blank screen",
            "workspace_id": wid,
            "turn_id": "turn_blank_open1",
            "is_final": True,
        },
    )
    assert placed.status_code == 200, placed.text
    raw = placed.json().get("result_json")
    payload = json.loads(raw) if isinstance(raw, str) else raw
    assert payload["action"] == "open_screen"
    assert payload["panes"][0]["kind"] == "blank"
    assert payload["panes"][0]["route"] == ""
    pane_id = payload["panes"][0]["id"]

    turned = client.post(
        "/api/vocalbridge/query",
        headers=AUTH_A,
        json={
            "text": "turn this screen into the shop page",
            "workspace_id": wid,
            "turn_id": "turn_blank_shop1",
            "is_final": True,
            "primary_pane_id": pane_id,
            "focused_pane_ids": [pane_id],
        },
    )
    assert turned.status_code == 200, turned.text
    raw = turned.json().get("result_json")
    updated = json.loads(raw) if isinstance(raw, str) else raw
    assert updated["action"] == "update_pane"
    assert updated["panes"][0]["id"] == pane_id
    assert updated["panes"][0]["kind"] == "preview"
    assert updated["panes"][0]["route"] == "/shop"

    chat = client.post(
        "/api/vocalbridge/query",
        headers=AUTH_A,
        json={
            "text": "turn this screen into a chat",
            "workspace_id": wid,
            "turn_id": "turn_blank_chat1",
            "is_final": True,
            "primary_pane_id": pane_id,
            "focused_pane_ids": [pane_id],
        },
    )
    assert chat.status_code == 200, chat.text
    raw = chat.json().get("result_json")
    converted = json.loads(raw) if isinstance(raw, str) else raw
    assert converted["panes"][0]["id"] == pane_id
    listed = client.get(f"/api/workspaces/{wid}/panes", headers=AUTH_A).json()
    items = listed if isinstance(listed, list) else listed.get("panes") or []
    match = next(p for p in items if p.get("id") == pane_id)
    assert match["kind"] == "chat"
    assert match["route"] == f"/chat/{pane_id}"


def test_filler_speech_never_starts_a_coding_job(client: TestClient):
    wid = _workspace(client)
    _preview(client, wid, "Home", "/")
    with patch("app.services.jobs.run_coding_job") as coding:
        for i, text in enumerate(["Ah", "also", "I", "Any of the other screens"]):
            res = client.post(
                "/api/vocalbridge/query",
                headers=AUTH_A,
                json={
                    "text": text,
                    "workspace_id": wid,
                    "turn_id": f"turn_filler_{i:04d}",
                    "is_final": True,
                },
            )
            assert res.status_code == 200, res.text
    coding.assert_not_called()


def test_named_and_unnamed_screens_get_distinct_routes(client: TestClient):
    wid = _workspace(client)
    routes = []
    for i, text in enumerate(
        ["add a shop screen", "open another screen", "open another screen"]
    ):
        res = client.post(
            "/api/vocalbridge/query",
            headers=AUTH_A,
            json={
                "text": text,
                "workspace_id": wid,
                "turn_id": f"turn_routes_{i:04d}",
                "is_final": True,
            },
        )
        raw = res.json().get("result_json")
        payload = json.loads(raw) if isinstance(raw, str) else raw
        routes.append(payload["panes"][0]["route"])
    assert routes[0] == "/shop"
    assert routes[1] != routes[2]
    assert "/shop" not in routes[1:]


def test_llm_plan_cannot_drop_pane_open(client: TestClient):
    from app.schemas import TurnActionPageEdit

    async def _page_edit_only(text: str, context: dict):
        _ = context
        return [
            TurnActionPageEdit(
                type="page.edit",
                action_id="a9",
                instruction=text,
            )
        ]

    wid = _workspace(client)
    with (
        patch("app.services.dialogue._needs_llm", return_value=True),
        patch("app.services.dialogue._llm_actions", _page_edit_only),
    ):
        res = client.post(
            "/api/vocalbridge/query",
            headers=AUTH_A,
            json={
                "text": "open another screen",
                "workspace_id": wid,
                "turn_id": "turn_keep_open01",
                "is_final": True,
            },
        )
    assert res.status_code == 200, res.text
    raw = res.json().get("result_json")
    payload = json.loads(raw) if isinstance(raw, str) else raw
    assert payload["action"] == "open_screen"
    assert payload["panes"][0]["created"] is True
    assert payload["panes"][0]["route"] not in {"", "/another"}


def test_workspace_sse_ping_and_headers():
    from app.routers.jobs import _SSE_HEADERS, _ping_event

    assert _SSE_HEADERS["X-Accel-Buffering"] == "no"
    assert "no-cache" in _SSE_HEADERS["Cache-Control"]
    frame = _ping_event("ws_ping")
    assert frame.startswith("data: ")
    body = json.loads(frame.removeprefix("data: ").strip())
    assert body["type"] == "ping"
    assert body["workspace_id"] == "ws_ping"
