"""Preview lifecycle orchestration → Vektral-Collab."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any
from urllib.parse import quote

from app.config import get_settings
from app.schemas import preview_view
from app.services import panes as panes_svc
from app.services.preview_client import preview_runner_request
from app.services.workspaces import (
    SITE_FAILED,
    SITE_READY,
    SITE_SEEDING,
    branch_name,
    github_token_for_user,
    require_site_ready,
    require_workspace_access,
    set_site_status,
)
from app.agent_kv import get_doc, now_iso, put_doc

logger = logging.getLogger("vektral.preview")


def _apply_runner_payload(sess: dict[str, Any], resp: dict[str, Any]) -> dict[str, Any]:
    runtime = resp.get("runtime") if isinstance(resp.get("runtime"), dict) else {}
    status = str(resp.get("status") or sess.get("status") or "stopped")
    if status == "running":
        status = "ready"
    if status == "failed":
        status = "error"
    sess["status"] = status
    sess["session_id"] = str(resp.get("session_id") or sess.get("session_id") or "")
    if "generation" in resp:
        sess["generation"] = int(resp.get("generation") or 0)
    origin = str(resp.get("preview_origin") or "")
    sess["preview_origin"] = origin
    sess["preview_base_url"] = origin
    sess["runtime"] = runtime
    sess["runtime_id"] = str(runtime.get("runtimeId") or sess.get("runtime_id") or "")
    if resp.get("commit_sha") is not None:
        sess["commit_sha"] = str(resp.get("commit_sha") or "")
    if resp.get("branch"):
        sess["branch"] = str(resp["branch"])
    if resp.get("logs") is not None:
        sess["logs"] = str(resp.get("logs") or "")
    if resp.get("error") is not None:
        sess["error"] = str(resp.get("error") or "")
    sess["error_code"] = str(resp.get("error_code") or runtime.get("errorCode") or "")
    sess["container_id"] = str(resp.get("container_id") or "")
    sess["port"] = int(resp.get("port") or 0)
    return sess


def _get_or_create_session(workspace_id: str) -> dict[str, Any]:
    existing = get_doc("preview_sessions", workspace_id)
    if existing:
        return existing
    now = now_iso()
    doc = {
        "id": workspace_id,
        "workspace_id": workspace_id,
        "status": "stopped",
        "branch": branch_name(workspace_id),
        "preview_base_url": "",
        "container_id": "",
        "port": 0,
        "commit_sha": "",
        "logs": "",
        "error": "",
        "created_at": now,
        "updated_at": now,
    }
    put_doc("preview_sessions", workspace_id, doc)
    return doc


def _save_session(sess: dict[str, Any]) -> dict[str, Any]:
    sess["updated_at"] = now_iso()
    put_doc("preview_sessions", sess["workspace_id"], sess)
    return sess


async def seed_starter_checkout(workspace_id: str, uid: str) -> dict[str, Any]:
    """Clone the GitHub repo and commit the Vite starter if the tree is empty.

    Does not start Docker. Voice coding (Mistral) still owns later Collab commits.
    Safe to call when there is no repo or GitHub token — those cases are skipped.
    """
    ws = require_workspace_access(workspace_id, uid)
    repo = str(ws.get("repo_full_name") or "").strip()
    if not repo:
        set_site_status(uid, workspace_id, status=SITE_READY)
        return {"ok": False, "skipped": True, "error": "no repo"}

    token = github_token_for_user(uid)
    if not token:
        token = get_settings().github_token_fallback
    if not token:
        logger.info("skip starter seed for %s: no GitHub token", workspace_id)
        set_site_status(
            uid,
            workspace_id,
            status=SITE_FAILED,
            error="no github token",
        )
        return {"ok": False, "skipped": True, "error": "no github token"}

    set_site_status(uid, workspace_id, status=SITE_SEEDING)
    branch = str(ws.get("vektral_branch") or "") or branch_name(workspace_id)
    sess = _get_or_create_session(workspace_id)
    sess["branch"] = branch
    sess["error"] = ""
    _save_session(sess)

    resp = await preview_runner_request(
        "POST",
        "/v1/git/seed_starter",
        {
            "workspace_id": workspace_id,
            "repo_full_name": repo,
            "default_branch": ws.get("default_branch") or "main",
            "branch": branch,
            "github_token": token,
        },
        timeout=180.0,
    )
    if not bool(resp.get("ok", False)):
        err = str(resp.get("error") or "starter seed failed")
        logger.warning("starter seed failed for %s: %s", workspace_id, err)
        sess["error"] = err
        sess["logs"] = str(resp.get("logs") or "")
        _save_session(sess)
        set_site_status(uid, workspace_id, status=SITE_FAILED, error=err)
        return {"ok": False, "skipped": False, "error": err}

    sess["commit_sha"] = str(resp.get("commit_sha") or "")
    sess["starter_seeded"] = bool(resp.get("starter_seeded"))
    sess["logs"] = str(resp.get("logs") or "")
    sess["error"] = ""
    _save_session(sess)
    set_site_status(
        uid,
        workspace_id,
        status=SITE_READY,
        commit_sha=sess["commit_sha"],
    )
    logger.info(
        "starter seed ok for %s seeded=%s sha=%s",
        workspace_id,
        sess["starter_seeded"],
        sess["commit_sha"],
    )
    return {
        "ok": True,
        "skipped": False,
        "commit_sha": sess["commit_sha"],
        "starter_seeded": sess["starter_seeded"],
    }


async def start_preview(workspace_id: str, uid: str) -> dict[str, Any]:
    ws = require_workspace_access(workspace_id, uid)
    require_site_ready(ws)
    sess = _get_or_create_session(workspace_id)
    if not ws.get("repo_full_name"):
        sess["status"] = "error"
        sess["error"] = "workspace has no GitHub repo"
        return preview_view(_save_session(sess))
    token = github_token_for_user(uid)
    if not token:
        # Allow empty token in stub: Collab will reject; surface clear error
        sess["status"] = "error"
        sess["error"] = "connect GitHub with repo scope first (or set users_github token)"
        # Still attempt if AUTH_DEV_BYPASS wants to hit Collab with env GH token
        from app.config import get_settings

        token = get_settings().github_token_fallback
        if not token:
            return preview_view(_save_session(sess))

    branch = ws.get("vektral_branch") or branch_name(workspace_id)
    sess["status"] = "starting"
    sess["branch"] = branch
    sess["error"] = ""
    _save_session(sess)

    resp = await preview_runner_request(
        "POST",
        "/v1/preview/start",
        {
            "workspace_id": workspace_id,
            "repo_full_name": ws["repo_full_name"],
            "default_branch": ws.get("default_branch") or "main",
            "branch": branch,
            "github_token": token,
        },
    )
    if not bool(resp.get("ok", False)):
        sess["status"] = "error"
        sess["error"] = str(resp.get("error", "preview start failed"))
        sess["error_code"] = str(resp.get("error_code") or "")
        sess["logs"] = str(resp.get("logs", "") or "")
        runtime = resp.get("runtime") if isinstance(resp.get("runtime"), dict) else {}
        if runtime:
            sess["runtime"] = runtime
        return preview_view(_save_session(sess))

    _apply_runner_payload(sess, resp)
    sess["starter_seeded"] = bool(resp.get("starter_seeded"))
    _save_session(sess)
    if isinstance(sess.get("runtime"), dict):
        panes_svc.attach_runtime_to_preview_panes(workspace_id, sess["runtime"])

    existing = panes_svc.list_panes(workspace_id, uid)
    if not any(p.get("kind") != "chat" and p.get("source_kind") != "external" for p in existing):
        panes_svc.create_pane(workspace_id, uid, title="Home", route="/")
        if isinstance(sess.get("runtime"), dict):
            panes_svc.attach_runtime_to_preview_panes(workspace_id, sess["runtime"])
    panes_svc.ensure_chat_pane(workspace_id, uid)
    return preview_view(sess)


async def restart_preview(workspace_id: str, uid: str) -> dict[str, Any]:
    ws = require_workspace_access(workspace_id, uid)
    sess = _get_or_create_session(workspace_id)
    token = github_token_for_user(uid) or get_settings().github_token_fallback
    branch = sess.get("branch") or ws.get("vektral_branch") or branch_name(workspace_id)
    resp = await preview_runner_request(
        "POST",
        "/v1/preview/restart",
        {
            "workspace_id": workspace_id,
            "repo_full_name": ws.get("repo_full_name") or "",
            "default_branch": ws.get("default_branch") or "main",
            "branch": branch,
            "github_token": token,
        },
    )
    if not bool(resp.get("ok", False)):
        sess["status"] = "error"
        sess["error"] = str(resp.get("error", "preview restart failed"))
        sess["error_code"] = str(resp.get("error_code") or "")
        sess["logs"] = str(resp.get("logs", "") or "")
        return preview_view(_save_session(sess))
    _apply_runner_payload(sess, resp)
    sess["error"] = ""
    _save_session(sess)
    if isinstance(sess.get("runtime"), dict):
        panes_svc.attach_runtime_to_preview_panes(workspace_id, sess["runtime"])
    return preview_view(sess)


async def preview_status(workspace_id: str, uid: str) -> dict[str, Any]:
    require_workspace_access(workspace_id, uid)
    sess = _get_or_create_session(workspace_id)
    resp = await preview_runner_request(
        "GET", f"/v1/preview/{workspace_id}/status", None
    )
    if resp.get("ok") is False and resp.get("error"):
        # Collab unreachable — return stored session
        return preview_view(sess)
    _apply_runner_payload(sess, resp)
    saved = _save_session(sess)
    if isinstance(saved.get("runtime"), dict):
        panes_svc.attach_runtime_to_preview_panes(workspace_id, saved["runtime"])
    return preview_view(saved)


async def preview_logs(workspace_id: str, uid: str) -> dict[str, Any]:
    require_workspace_access(workspace_id, uid)
    sess = _get_or_create_session(workspace_id)
    resp = await preview_runner_request(
        "GET", f"/v1/preview/{workspace_id}/logs", None
    )
    if resp.get("logs") is not None:
        sess["logs"] = str(resp.get("logs") or "")
    if resp.get("status"):
        sess["status"] = str(resp["status"])
    return preview_view(_save_session(sess))


async def wait_preview_routes(
    workspace_id: str,
    routes: list[str],
    *,
    timeout: float | None = None,
) -> bool:
    """Block until Collab reports runtime ready and each route answers HTTP."""
    s = get_settings()
    budget = float(timeout if timeout is not None else s.preview_ready_timeout_seconds)
    deadline = time.time() + max(1.0, budget)
    unique_routes: list[str] = []
    for route in routes or ["/"]:
        path = str(route or "/").strip() or "/"
        if not path.startswith("/"):
            path = f"/{path}"
        if path not in unique_routes:
            unique_routes.append(path)
    while time.time() < deadline:
        status_resp = await preview_runner_request(
            "GET", f"/v1/preview/{workspace_id}/status", None, timeout=10.0
        )
        status = str(status_resp.get("status") or "")
        if status == "error":
            return False
        if status == "ready":
            all_ok = True
            for path in unique_routes:
                ready = await preview_runner_request(
                    "GET",
                    f"/v1/preview/{workspace_id}/readiness?route={quote(path)}",
                    None,
                    timeout=10.0,
                )
                if not (bool(ready.get("ready")) and bool(ready.get("route_ready", True))):
                    all_ok = False
                    break
            if all_ok:
                return True
        await asyncio.sleep(0.4)
    return False


async def launch_pane(workspace_id: str, pane_id: str, uid: str) -> dict[str, Any]:
    from fastapi import HTTPException

    from app.services.preview_tickets import issue_launch_ticket

    require_workspace_access(workspace_id, uid)
    pane = panes_svc.find_pane(pane_id)
    if pane is None or pane.get("workspace_id") != workspace_id:
        raise HTTPException(status_code=404, detail={"error": "not_found", "message": "pane not found"})
    if panes_svc.is_chat_pane(pane):
        raise HTTPException(status_code=400, detail={"error": "invalid_kind", "message": "chat panes have no preview origin"})
    if panes_svc.is_blank_pane(pane):
        raise HTTPException(
            status_code=400,
            detail={"error": "invalid_kind", "message": "blank panes have no surface until converted"},
        )
    if str(pane.get("source_kind") or "workspace-preview") == "external":
        return {
            "launch_url": "",
            "preview_url": str(pane.get("external_url") or ""),
            "source_kind": "external",
            "unsupported_reason": pane.get("unsupported_reason") or "external_frame_policy",
            "status": "unsupported",
        }
    sess = _get_or_create_session(workspace_id)
    status = str(sess.get("status") or "")
    if status != "ready":
        await preview_status(workspace_id, uid)
        sess = _get_or_create_session(workspace_id)
        status = str(sess.get("status") or "")
    if status != "ready":
        raise HTTPException(
            status_code=409,
            detail={"error": status or "not_ready", "message": "preview is not ready"},
        )
    runtime = sess.get("runtime") if isinstance(sess.get("runtime"), dict) else {}
    session_id = panes_svc.pane_preview_session_id(pane)
    reload_v = int(pane.get("reload_version") or 0)
    ticket = issue_launch_ticket(
        uid=uid,
        workspace_id=workspace_id,
        pane_id=pane_id,
        runtime_id=str(sess.get("runtime_id") or runtime.get("runtimeId") or ""),
        route=str(pane.get("route") or "/"),
        session_id=session_id or str(sess.get("session_id") or ""),
        generation=int(sess.get("generation") or runtime.get("generation") or 0),
        reload_version=reload_v,
    )
    pane["preview_status"] = status
    pane["launch_url"] = ""
    pane["preview_url"] = ""
    pane["updated_at"] = now_iso()
    put_doc("panes", pane_id, pane)
    return {
        **ticket,
        "status": status,
        "reload_version": reload_v,
        "pane": panes_svc.pane_view(pane, str(ticket.get("preview_origin") or "")),
    }
