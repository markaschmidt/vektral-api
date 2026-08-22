"""Preview lifecycle orchestration → Vektral-Collab."""

from __future__ import annotations

import logging
from typing import Any

from app.config import get_settings
from app.schemas import preview_view
from app.services import panes as panes_svc
from app.services.preview_client import preview_runner_request
from app.services.workspaces import (
    branch_name,
    github_token_for_user,
    require_workspace_access,
)
from app.agent_kv import get_doc, now_iso, put_doc

logger = logging.getLogger("vektral.preview")


def _public_base(workspace_id: str, port: int) -> str:
    s = get_settings()
    public = s.preview_public_base.rstrip("/")
    if public:
        return f"{public}/{workspace_id}"
    if port > 0:
        return f"{s.preview_runner_public_host.rstrip('/')}:{port}"
    return ""


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
        return {"ok": False, "skipped": True, "error": "no repo"}

    token = github_token_for_user(uid)
    if not token:
        token = get_settings().github_token_fallback
    if not token:
        logger.info("skip starter seed for %s: no GitHub token", workspace_id)
        return {"ok": False, "skipped": True, "error": "no github token"}

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
        return {"ok": False, "skipped": False, "error": err}

    sess["commit_sha"] = str(resp.get("commit_sha") or "")
    sess["starter_seeded"] = bool(resp.get("starter_seeded"))
    sess["logs"] = str(resp.get("logs") or "")
    sess["error"] = ""
    _save_session(sess)
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
    sess = _get_or_create_session(workspace_id)
    if not ws.get("repo_full_name"):
        sess["status"] = "failed"
        sess["error"] = "workspace has no GitHub repo"
        return preview_view(_save_session(sess))
    token = github_token_for_user(uid)
    if not token:
        # Allow empty token in stub: Collab will reject; surface clear error
        sess["status"] = "failed"
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
        sess["status"] = "failed"
        sess["error"] = str(resp.get("error", "preview start failed"))
        sess["logs"] = str(resp.get("logs", "") or "")
        return preview_view(_save_session(sess))

    sess["status"] = str(resp.get("status", "running") or "running")
    sess["port"] = int(resp.get("port", 0) or 0)
    sess["container_id"] = str(resp.get("container_id", "") or "")
    sess["commit_sha"] = str(resp.get("commit_sha", "") or "")
    sess["logs"] = str(resp.get("logs", "") or "")
    sess["starter_seeded"] = bool(resp.get("starter_seeded"))
    base = _public_base(workspace_id, sess["port"])
    if resp.get("preview_base_url"):
        base = str(resp["preview_base_url"])
    sess["preview_base_url"] = base
    _save_session(sess)

    existing = panes_svc.list_panes(workspace_id, uid)
    if not any(p.get("kind") != "chat" for p in existing):
        panes_svc.create_pane(workspace_id, uid, title="Home", route="/")
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
        sess["status"] = "failed"
        sess["error"] = str(resp.get("error", "preview restart failed"))
        sess["logs"] = str(resp.get("logs", "") or "")
        return preview_view(_save_session(sess))
    sess["status"] = str(resp.get("status", "running") or "running")
    sess["port"] = int(resp.get("port", sess.get("port") or 0) or 0)
    sess["container_id"] = str(resp.get("container_id", "") or "")
    sess["commit_sha"] = str(resp.get("commit_sha", "") or "")
    sess["logs"] = str(resp.get("logs", "") or "")
    if resp.get("preview_base_url"):
        sess["preview_base_url"] = str(resp["preview_base_url"])
    elif sess["port"]:
        sess["preview_base_url"] = _public_base(workspace_id, int(sess["port"]))
    sess["error"] = ""
    return preview_view(_save_session(sess))


async def preview_status(workspace_id: str, uid: str) -> dict[str, Any]:
    require_workspace_access(workspace_id, uid)
    sess = _get_or_create_session(workspace_id)
    resp = await preview_runner_request(
        "GET", f"/v1/preview/{workspace_id}/status", None
    )
    if resp.get("ok") is False and resp.get("error"):
        # Collab unreachable — return stored session
        return preview_view(sess)
    sess["status"] = str(resp.get("status", sess.get("status") or "stopped"))
    if "port" in resp:
        sess["port"] = int(resp.get("port") or 0)
    if resp.get("container_id") is not None:
        sess["container_id"] = str(resp.get("container_id") or "")
    if resp.get("commit_sha") is not None:
        sess["commit_sha"] = str(resp.get("commit_sha") or "")
    if resp.get("branch"):
        sess["branch"] = str(resp["branch"])
    if resp.get("preview_base_url"):
        sess["preview_base_url"] = str(resp["preview_base_url"])
    if resp.get("logs") is not None:
        sess["logs"] = str(resp.get("logs") or "")
    if resp.get("error") is not None:
        sess["error"] = str(resp.get("error") or "")
    return preview_view(_save_session(sess))


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
