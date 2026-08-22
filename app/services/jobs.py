"""Agent jobs — queue, research/coding execution, SSE helpers.

MVP: jobs run inline (or via FastAPI BackgroundTasks). For multi-replica
production, move execution to Celery/RQ + Redis and keep Firestore as the
source of truth for job status.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from app.schemas import job_view
from app.services import panes as panes_svc
from app.services.coding import run_coding_job
from app.services.linear_jobs import is_linear_command, run_linear_job
from app.services.models_catalog import resolve_openrouter_model
from app.services.research import is_research_command, run_research_job
from app.services.workspaces import get_workspace, require_workspace_access
from app.agent_kv import get_doc, new_id, now_iso, put_doc, query_eq

logger = logging.getLogger("vektral.jobs")


def create_job_record(
    *,
    owner_uid: str,
    status: str = "queued",
    command_type: str = "text",
    command_text: str = "",
    workspace_id: str = "",
    pane_id: str = "",
    chat_pane_id: str = "",
    route: str = "/",
    model: str = "",
    payload_json: str = "",
    result_json: str = "",
    error: str = "",
) -> dict[str, Any]:
    now = now_iso()
    jid = new_id("job_")
    doc = {
        "id": jid,
        "owner_uid": owner_uid,
        "status": status,
        "command_type": command_type,
        "command_text": command_text,
        "workspace_id": workspace_id,
        "pane_id": pane_id,
        "chat_pane_id": chat_pane_id,
        "route": route or "/",
        "model": model,
        "branch": "",
        "commit_sha": "",
        "preview_url": "",
        "reload_version": 0,
        "logs": "",
        "payload_json": payload_json,
        "result_json": result_json,
        "error": error,
        "created_at": now,
        "updated_at": now,
    }
    put_doc("jobs", jid, doc)
    return doc


def get_job(job_id: str, uid: str | None = None) -> dict[str, Any] | None:
    doc = get_doc("jobs", job_id)
    if doc is None:
        return None
    if uid and doc.get("owner_uid") and doc.get("owner_uid") != uid:
        return None
    return job_view(doc)


def list_workspace_jobs(workspace_id: str) -> list[dict[str, Any]]:
    return query_eq("jobs", "workspace_id", workspace_id)


def _failed(
    uid: str,
    command: dict[str, Any],
    workspace_id: str,
    pane_id: str,
    route: str,
    model: str,
    error: str,
) -> dict[str, Any]:
    doc = create_job_record(
        owner_uid=uid,
        status="failed",
        command_type=command.get("type") or "text",
        command_text=command.get("text") or "",
        workspace_id=workspace_id,
        pane_id=pane_id,
        route=route,
        model=model,
        payload_json=command.get("payload_json") or "",
        error=error,
    )
    return job_view(doc)


def _assistant_text(job: dict[str, Any]) -> str:
    err = str(job.get("error") or "").strip()
    if err:
        return err
    raw = job.get("result_json") or ""
    if isinstance(raw, dict):
        data = raw
    else:
        try:
            data = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            data = {}
    if isinstance(data, dict):
        summary = str(data.get("summary") or "").strip()
        if summary:
            return summary
        title = str(data.get("title") or "").strip()
        url = str(data.get("url") or "").strip()
        if title or url:
            return f"Researched {title or url}"
    logs = str(job.get("logs") or "").strip().split("\n")
    if logs and logs[-1]:
        return logs[-1][:1500]
    return str(job.get("status") or "done")


async def submit_command(uid: str, command: dict[str, Any]) -> dict[str, Any]:
    text = command.get("text") or ""
    ctype = command.get("type") or "text"
    if not text and not ctype:
        return _failed(uid, command, "", "", "", "", "command type or text is required")

    workspace_id = command.get("workspace_id") or ""
    route = command.get("route") or ""
    pane_id = command.get("pane_id") or ""
    new_conversation = bool(command.get("new_conversation"))

    if pane_id and (not workspace_id or not route):
        pane = panes_svc.find_pane(pane_id)
        if pane:
            workspace_id = workspace_id or pane.get("workspace_id") or ""
            route = route or pane.get("route") or ""

    if workspace_id:
        try:
            require_workspace_access(workspace_id, uid)
        except Exception:  # noqa: BLE001 — HTTPException or missing
            ws = get_workspace(workspace_id)
            if ws is None or ws.get("owner_uid") != uid:
                return _failed(
                    uid,
                    command,
                    workspace_id,
                    pane_id,
                    route,
                    command.get("model") or "",
                    "workspace not found or forbidden",
                )

    chat_pane_id = ""
    if workspace_id:
        try:
            resolved = panes_svc.resolve_chat_pane(
                uid,
                workspace_id,
                pane_id=pane_id,
                new_conversation=new_conversation,
            )
            chat_pane_id = str(resolved.get("chat_pane_id") or "")
            preview_route = str(resolved.get("preview_route") or "")
            if not pane_id:
                pane_id = chat_pane_id
            if (
                not route
                or str(route).startswith("/chat")
                or panes_svc.is_chat_pane(panes_svc.find_pane(pane_id))
            ):
                route = preview_route or route
            if chat_pane_id and text:
                panes_svc.append_message(chat_pane_id, uid, "user", text)
        except Exception:  # noqa: BLE001
            logger.exception("chat pane resolve failed for workspace %s", workspace_id)

    resolved = resolve_openrouter_model(command.get("model") or "")
    if resolved.get("ok") != "true":
        return _failed(
            uid,
            command,
            workspace_id,
            pane_id,
            route,
            command.get("model") or "",
            str(resolved.get("error", "model not allowed")),
        )
    use_model = resolved.get("model") or ""

    research = ctype == "research" or (
        ctype in ("text", "voice") and is_research_command(text)
    )
    linear = ctype == "linear" or (
        ctype in ("text", "voice") and not research and is_linear_command(text)
    )
    job = create_job_record(
        owner_uid=uid,
        status="queued",
        command_type="research" if research else ("linear" if linear else ctype),
        command_text=text,
        workspace_id=workspace_id,
        pane_id=pane_id,
        chat_pane_id=chat_pane_id,
        route=route or "/",
        model=use_model,
        payload_json=command.get("payload_json") or "",
    )

    if research:
        job = await run_research_job(job, uid)
    elif linear:
        job = await run_linear_job(job, uid)
    elif ctype in ("text", "voice", "code", "edit", "agent"):
        job = await run_coding_job(job, uid)
    else:
        job["status"] = "queued"
        job["updated_at"] = now_iso()
        put_doc("jobs", job["id"], job)

    if chat_pane_id:
        panes_svc.append_message(
            chat_pane_id,
            uid,
            "assistant",
            _assistant_text(job),
            job_id=str(job.get("id") or ""),
        )

    return job_view(job)


async def submit_pane_command(
    uid: str,
    pane_id: str,
    text: str,
    type_: str = "text",
    model: str = "",
    payload_json: str = "",
    new_conversation: bool = False,
) -> dict[str, Any]:
    pane = panes_svc.find_pane(pane_id)
    workspace_id = (pane or {}).get("workspace_id") or ""
    route = (pane or {}).get("route") or "/"
    return await submit_command(
        uid,
        {
            "type": type_,
            "text": text,
            "workspace_id": workspace_id,
            "pane_id": pane_id,
            "route": route,
            "model": model,
            "payload_json": payload_json,
            "new_conversation": new_conversation,
        },
    )
