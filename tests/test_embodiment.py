"""Embodiment context, plans, receipts, SSE, and fixture contracts."""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

os.environ["ALLOW_DEV_BEARER"] = "true"
os.environ["VEKTRAL_USE_MEMORY_STORE"] = "true"
os.environ["FIREBASE_STUB_MODE"] = "true"
os.environ["EMBODIMENT_ENABLED"] = "true"
os.environ["DIALOGUE_ORCHESTRATION_ENABLED"] = "false"
os.environ.pop("GOOGLE_APPLICATION_CREDENTIALS", None)

FIXTURES = (
    Path(__file__).resolve().parents[2] / "vektral-contracts" / "fixtures" / "embodiment"
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


def _workspace(client: TestClient) -> str:
    res = client.post(
        "/api/workspaces",
        headers=AUTH_A,
        json={"name": "Embodiment WS", "description": "npc"},
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


def test_fixtures_match_pydantic_and_apex_intent():
    from app.schemas import AgentContextSnapshot, AgentPlanEnvelope, EmbodimentReceipt

    ctx = AgentContextSnapshot.model_validate_json(
        (FIXTURES / "context_snapshot.json").read_text()
    )
    assert ctx.primary_pane_id == "pane_home"
    assert ctx.focused_pane_ids[0] == "pane_home"

    envelope = AgentPlanEnvelope.model_validate_json(
        (FIXTURES / "attend_plan.json").read_text()
    )
    apex = json.loads((FIXTURES / "apex_execute_plan.json").read_text())
    assert envelope.apex_intent() == apex

    receipt = EmbodimentReceipt.model_validate_json(
        (FIXTURES / "receipt_completed.json").read_text()
    )
    assert receipt.status == "completed"


def test_openapi_lists_embodiment_events_without_tokens():
    text = OPENAPI.read_text()
    assert "/api/workspaces/{workspace_id}/agent-context" in text
    assert "embodiment_plan" in (
        Path(__file__).resolve().parents[2]
        / "vektral-contracts"
        / "VR_PANE_LIVE_UPDATES.md"
    ).read_text()
    assert "embodiment_cancel" in text or "EmbodimentReceipt" in text
    assert "id_token" not in text.lower()
    from app.routers.jobs import _embodiment_sse, _job_event_payload

    payload = json.loads(
        _job_event_payload(
            {
                "id": "job_1",
                "status": "ready",
                "logs": "secret",
                "embodiment_json": {"plan_id": "plan_1", "revision": 1},
            }
        )
    )
    blob = json.dumps(payload)
    assert "Bearer" not in blob
    assert "token" not in blob.lower()
    event = _embodiment_sse(
        "embodiment_plan",
        workspace_id="ws_1",
        plan={"plan_id": "plan_1", "revision": 1, "intent": {"type": "attend"}},
        job_id="job_1",
    )
    assert event["type"] == "embodiment_plan"
    assert "token" not in event["payload_json"].lower()


def test_context_authorization_and_pane_validation(client: TestClient):
    wid = _workspace(client)
    home = _preview(client, wid, "Home", "/")
    foreign = client.post(
        "/api/workspaces",
        headers=AUTH_B,
        json={"name": "Bob", "description": "x"},
    ).json()["id"]
    bob_pane = client.post(
        f"/api/workspaces/{foreign}/panes",
        headers=AUTH_B,
        json={"title": "Bob home", "route": "/"},
    ).json()

    denied = client.put(
        f"/api/workspaces/{wid}/agent-context",
        headers=AUTH_B,
        json={"client_revision": 1, "focused_pane_ids": [home["id"]]},
    )
    assert denied.status_code in (403, 404)

    missing = client.put(
        f"/api/workspaces/{wid}/agent-context",
        headers=AUTH_A,
        json={"client_revision": 1, "focused_pane_ids": ["pane_nope"]},
    )
    assert missing.status_code == 400

    stolen = client.put(
        f"/api/workspaces/{wid}/agent-context",
        headers=AUTH_A,
        json={"client_revision": 1, "focused_pane_ids": [bob_pane["id"]]},
    )
    assert stolen.status_code == 400

    spatial = client.put(
        f"/api/workspaces/{wid}/agent-context",
        headers=AUTH_A,
        json={"client_revision": 1, "position": {"x": 1, "y": 0, "z": 2}},
    )
    assert spatial.status_code == 422

    loco = client.put(
        f"/api/workspaces/{wid}/agent-context",
        headers=AUTH_A,
        json={
            "client_revision": 1,
            "primary_pane_id": home["id"],
            "focused_pane_ids": [home["id"]],
            "npc_phase": "idle",
            "locomotion_substate": "attending",
            "clip": "idle",
            "presence": "idle",
            "moving": False,
            "phase": "idle",
            "substate": "attending",
        },
    )
    assert loco.status_code == 200, loco.text
    assert loco.json()["locomotion_substate"] == "attending"

    data_url = client.put(
        f"/api/workspaces/{wid}/agent-context",
        headers=AUTH_A,
        json={
            "client_revision": 1,
            "focused_pane_ids": [home["id"]],
            "image_refs": ["data:image/png;base64,abcd"],
        },
    )
    assert data_url.status_code == 422


def test_context_revision_and_ttl(client: TestClient):
    from app.agent_kv import get_doc, put_doc

    wid = _workspace(client)
    home = _preview(client, wid, "Home", "/")
    about = _preview(client, wid, "About", "/about")
    body = {
        "client_revision": 2,
        "primary_pane_id": home["id"],
        "focused_pane_ids": [home["id"], about["id"]],
        "visible_panes": [
            {
                "pane_id": home["id"],
                "distance": "near",
                "direction": "front",
                "visible": True,
            }
        ],
    }
    ok = client.put(
        f"/api/workspaces/{wid}/agent-context",
        headers=AUTH_A,
        json=body,
    )
    assert ok.status_code == 200
    assert ok.json()["focused_pane_ids"] == [home["id"], about["id"]]
    assert ok.json()["server_revision"] == 1

    stale = client.put(
        f"/api/workspaces/{wid}/agent-context",
        headers=AUTH_A,
        json={**body, "client_revision": 1},
    )
    assert stale.status_code == 409

    doc = get_doc("embodiment_context", f"embctx_{wid}_alice")
    assert doc is not None
    doc["expires_at"] = "2000-01-01T00:00:00+00:00"
    put_doc("embodiment_context", doc["id"], doc)
    state = client.get(
        f"/api/workspaces/{wid}/embodiment/state",
        headers=AUTH_A,
    )
    assert state.status_code == 200
    assert state.json()["context"] is None

    restarted = client.put(
        f"/api/workspaces/{wid}/agent-context",
        headers=AUTH_A,
        json={**body, "client_revision": 1},
    )
    assert restarted.status_code == 200, restarted.text
    assert restarted.json()["client_revision"] == 1


def test_deterministic_plan_and_receipts(client: TestClient):
    wid = _workspace(client)
    home = _preview(client, wid, "Home", "/")
    client.put(
        f"/api/workspaces/{wid}/agent-context",
        headers=AUTH_A,
        json={
            "client_revision": 1,
            "primary_pane_id": home["id"],
            "focused_pane_ids": [home["id"]],
        },
    )
    job = client.post(
        "/api/submit_command",
        headers=AUTH_A,
        json={
            "text": "make the hero green",
            "workspace_id": wid,
            "context_revision": 1,
            "primary_pane_id": home["id"],
            "focused_pane_ids": [home["id"]],
        },
    )
    assert job.status_code == 200
    plan = job.json().get("embodiment_json") or {}
    assert plan.get("plan_id")
    assert plan["intent"]["pane_ids"] == [home["id"]]
    assert plan["apex_intent"]["type"] == "execute_plan"
    assert "position" not in json.dumps(plan)
    assert plan["apex_intent"]["steps"][0]["type"] == "attend"

    receipt = client.post(
        f"/api/workspaces/{wid}/embodiment/receipts",
        headers=AUTH_A,
        json={
            "plan_id": plan["plan_id"],
            "revision": plan["revision"],
            "status": "completed",
            "actual_primary_pane_id": home["id"],
            "context_revision": 1,
        },
    )
    assert receipt.status_code == 200

    stale = client.post(
        f"/api/workspaces/{wid}/embodiment/receipts",
        headers=AUTH_A,
        json={
            "plan_id": plan["plan_id"],
            "revision": plan["revision"] - 1 if plan["revision"] > 1 else 0,
            "status": "completed",
        },
    )
    assert stale.status_code in (409, 422)

    wrong = client.post(
        f"/api/workspaces/{wid}/embodiment/receipts",
        headers=AUTH_A,
        json={"plan_id": "plan_other", "revision": plan["revision"], "status": "cancelled"},
    )
    assert wrong.status_code == 409


def test_planner_invalid_json_falls_back(client: TestClient):
    wid = _workspace(client)
    home = _preview(client, wid, "Home", "/")
    about = _preview(client, wid, "About", "/about")
    with patch(
        "app.services.coding._llm_chat",
        new=AsyncMock(
            return_value={
                "ok": True,
                "content": '{"pane_ids":["x"],"position":{"x":1,"y":0,"z":0}}',
            }
        ),
    ):
        job = client.post(
            "/api/submit_command",
            headers=AUTH_A,
            json={
                "text": "wave and present both screens",
                "workspace_id": wid,
                "focused_pane_ids": [home["id"], about["id"]],
                "primary_pane_id": home["id"],
                "context_revision": 3,
            },
        )
    assert job.status_code == 200
    plan = job.json()["embodiment_json"]
    assert plan["intent"]["pane_ids"] == [home["id"], about["id"]]
    assert plan["intent"]["primary_pane_id"] == home["id"]


def test_planner_orders_multi_focus(client: TestClient):
    wid = _workspace(client)
    home = _preview(client, wid, "Home", "/")
    about = _preview(client, wid, "About", "/about")
    with patch(
        "app.services.coding._llm_chat",
        new=AsyncMock(
            return_value={
                "ok": True,
                "content": json.dumps(
                    {
                        "pane_ids": [about["id"], home["id"]],
                        "primary_pane_id": about["id"],
                        "phase_hint": "speaking",
                        "clip_hint": "talk",
                        "gaze_hint": "cycle",
                    }
                ),
            }
        ),
    ):
        job = client.post(
            "/api/submit_command",
            headers=AUTH_A,
            json={
                "text": "greet the user and talk through both screens",
                "workspace_id": wid,
                "focused_pane_ids": [home["id"], about["id"]],
                "primary_pane_id": home["id"],
            },
        )
    plan = job.json()["embodiment_json"]
    assert plan["intent"]["pane_ids"][0] == about["id"]
    assert plan["intent"]["clip_hint"] == "talk"


def test_receipt_cancel_and_job_sse_coexist(client: TestClient):
    wid = _workspace(client)
    home = _preview(client, wid, "Home", "/")
    job = client.post(
        "/api/submit_command",
        headers=AUTH_A,
        json={
            "text": "nudge the title",
            "workspace_id": wid,
            "focused_pane_ids": [home["id"]],
            "primary_pane_id": home["id"],
        },
    ).json()
    plan = job["embodiment_json"]
    assert job["status"] in ("failed", "queued", "ready")
    cancel = client.post(
        f"/api/workspaces/{wid}/embodiment/receipts",
        headers=AUTH_A,
        json={
            "plan_id": plan["plan_id"],
            "revision": plan["revision"],
            "status": "cancelled",
            "blocked_reason": "focus_off",
        },
    )
    assert cancel.status_code == 200
    assert cancel.json()["cancelled"] is True

    types: list[str] = []
    with client.stream(
        "GET", f"/api/jobs/{job['id']}/events", headers=AUTH_A
    ) as stream:
        buf = ""
        for chunk in stream.iter_text():
            buf += chunk
            for line in buf.split("\n"):
                if line.startswith("data: "):
                    try:
                        types.append(json.loads(line[6:]).get("type") or "")
                    except json.JSONDecodeError:
                        continue
            if "job_failed" in buf or "pane_reload" in buf or "timeout" in buf:
                break
    assert "embodiment_cancel" in types or "embodiment_plan" in types
    assert "job_update" in types
    assert "job_failed" in types or "pane_reload" in types


def test_research_and_linear_routing_unchanged(client: TestClient):
    wid = _workspace(client)
    home = _preview(client, wid, "Home", "/")
    research = client.post(
        "/api/submit_command",
        headers=AUTH_A,
        json={
            "text": "research https://example.com/docs",
            "workspace_id": wid,
            "focused_pane_ids": [home["id"]],
            "primary_pane_id": home["id"],
        },
    )
    assert research.status_code == 200
    assert research.json()["command_type"] == "research"
    assert research.json()["embodiment_json"].get("plan_id")

    linear = client.post(
        "/api/submit_command",
        headers=AUTH_A,
        json={
            "text": "create an issue for the login bug",
            "workspace_id": wid,
            "focused_pane_ids": [home["id"]],
            "primary_pane_id": home["id"],
        },
    )
    assert linear.status_code == 200
    assert linear.json()["command_type"] == "linear"


def test_disabled_flag_skips_job_plan(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ALLOW_DEV_BEARER", "true")
    monkeypatch.setenv("VEKTRAL_USE_MEMORY_STORE", "true")
    monkeypatch.setenv("FIREBASE_STUB_MODE", "true")
    monkeypatch.setenv("EMBODIMENT_ENABLED", "false")

    from app.agent_kv import clear_memory
    from app.config import get_settings
    from app.store import reset_memory_store, set_store_override

    get_settings.cache_clear()
    set_store_override(None)
    reset_memory_store()
    clear_memory()
    from app.main import app

    with TestClient(app) as c:
        wid = _workspace(c)
        home = _preview(c, wid, "Home", "/")
        job = c.post(
            "/api/submit_command",
            headers=AUTH_A,
            json={
                "text": "hello",
                "workspace_id": wid,
                "focused_pane_ids": [home["id"]],
            },
        )
        assert job.status_code == 200
        assert job.json().get("embodiment_json") in ({}, None)
    clear_memory()
    set_store_override(None)
    get_settings.cache_clear()


def test_execute_queued_job_is_isolated():
    from app.services import jobs as jobs_svc

    assert callable(jobs_svc.execute_queued_job)
    assert callable(jobs_svc.attach_embodiment_plan)


def test_walk_command_uses_default_preview_without_focused_ids(client: TestClient):
    wid = _workspace(client)
    home = _preview(client, wid, "Home", "/")
    with patch("app.services.jobs.run_coding_job") as coding:
        job = client.post(
            "/api/vocalbridge/query",
            headers=AUTH_A,
            json={"text": "walk over to that screen", "workspace_id": wid},
        )
    assert job.status_code == 200
    body = job.json()
    assert body["status"] == "completed"
    plan = body.get("embodiment_json") or {}
    assert plan.get("plan_id")
    assert plan["intent"]["pane_ids"] == [home["id"]]
    assert plan["intent"]["clip_hint"] == "walk"
    assert plan["apex_intent"]["type"] == "execute_plan"
    coding.assert_not_called()


def test_product_edit_is_not_attend_only():
    from app.services.embodiment import is_attend_only_command

    assert is_attend_only_command("walk over to the homepage")
    assert is_attend_only_command("come here")
    assert not is_attend_only_command("make the hero green")
    assert not is_attend_only_command("walk over and change the checkout button")


def test_preview_capabilities_isolated_from_avatar(client: TestClient):
    from app.schemas import AvatarStepGesture, PreviewCapabilities
    from app.services.embodiment import filter_avatar_steps

    wid = _workspace(client)
    home = _preview(client, wid, "Home", "/")
    body = {
        "client_revision": 1,
        "primary_pane_id": home["id"],
        "focused_pane_ids": [home["id"]],
        "capabilities": ["avatar.plan.v2"],
        "preview": {
            "container": "iframe",
            "in_world": True,
            "interactive": True,
            "arbitrary_origins": False,
            "video": False,
            "keyboard": True,
        },
    }
    ok = client.put(
        f"/api/workspaces/{wid}/agent-context",
        headers=AUTH_A,
        json=body,
    )
    assert ok.status_code == 200
    ctx = ok.json()
    assert ctx["preview"]["container"] == "iframe"
    assert ctx["capabilities"] == ["avatar.plan.v2"]
    steps, warnings = filter_avatar_steps(
        [AvatarStepGesture(type="gesture", gesture="wave")],
        ctx["capabilities"],
    )
    assert steps
    assert not warnings
    _ = PreviewCapabilities
