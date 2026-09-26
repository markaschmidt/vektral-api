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
from app.services import embodiment as emb_svc
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
        "embodiment_json": "",
        "screen_context": "",
        "image_refs": [],
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
    agent = str(job.get("agent_response") or "").strip()
    if agent:
        return agent
    return str(job.get("status") or "done")


def attach_query_fields(
    view: dict[str, Any],
    *,
    turn: dict[str, Any] | None = None,
    duplicate: bool = False,
) -> dict[str, Any]:
    """Additive VocalBridge/native fields: turn_id, duplicate, agent_response."""
    out = dict(view)
    turn_id = ""
    summary = ""
    duplicate = duplicate or bool(out.get("duplicate"))
    if turn:
        turn_id = str(turn.get("turn_id") or "")
        summary = str(turn.get("summary") or "")
        duplicate = duplicate or bool(turn.get("duplicate"))
    if not turn_id:
        nested = out.get("dialogue_turn") if isinstance(out.get("dialogue_turn"), dict) else {}
        turn_id = str(nested.get("turn_id") or out.get("turn_id") or "")
    if not summary:
        summary = _assistant_text(out)
    out["turn_id"] = turn_id
    out["duplicate"] = bool(duplicate)
    out["agent_response"] = summary[:1500]
    return out


def _raw_plan(job: dict[str, Any]) -> dict[str, Any]:
    raw = job.get("embodiment_json") or ""
    if isinstance(raw, dict):
        return raw
    try:
        parsed = json.loads(raw) if raw else {}
    except (json.JSONDecodeError, TypeError):
        parsed = {}
    return parsed if isinstance(parsed, dict) else {}


async def execute_queued_job(job: dict[str, Any], uid: str) -> dict[str, Any]:
    """Run research/linear/coding. Isolated so dispatch can move to Redis/RQ later."""
    ctype = str(job.get("command_type") or "text")
    if ctype == "research":
        return await run_research_job(job, uid)
    if ctype == "linear":
        return await run_linear_job(job, uid)
    if ctype in ("text", "voice", "code", "edit", "agent"):
        return await run_coding_job(job, uid)
    job["status"] = "queued"
    job["updated_at"] = now_iso()
    put_doc("jobs", job["id"], job)
    return job


async def attach_embodiment_plan(
    uid: str,
    job: dict[str, Any],
    command: dict[str, Any],
) -> dict[str, Any]:
    workspace_id = str(job.get("workspace_id") or command.get("workspace_id") or "")
    if not workspace_id:
        return job
    try:
        screen = emb_svc.screen_context_for_prompt(uid, workspace_id, command)
        if screen:
            job["screen_context"] = screen
        refs = emb_svc.image_refs_for_command(uid, workspace_id, command)
        if refs:
            job["image_refs"] = refs
        plan = await emb_svc.plan_for_command(uid, workspace_id, job, command)
        if plan:
            job["embodiment_json"] = json.dumps(plan)
        if screen or refs or plan:
            job["updated_at"] = now_iso()
            put_doc("jobs", job["id"], job)
    except Exception:  # noqa: BLE001 — coding must not fail because of embodiment
        logger.exception("embodiment attach failed for job %s", job.get("id"))
    return job


async def submit_command(uid: str, command: dict[str, Any]) -> dict[str, Any]:
    from app.services import dialogue as dialogue_svc

    if dialogue_svc.orchestration_enabled():
        adapted = await dialogue_svc.submit_from_legacy_command(uid, command)
        if adapted is not None:
            return attach_query_fields(adapted)
    return attach_query_fields(await _legacy_submit_command(uid, command))


async def _legacy_submit_command(uid: str, command: dict[str, Any]) -> dict[str, Any]:
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
    screen_cmd = (
        not research
        and not linear
        and bool(workspace_id)
        and panes_svc.is_screen_command(text)
        and not command.get("_skip_screen")
    )
    crud_cmd = (
        not research
        and not linear
        and not screen_cmd
        and bool(workspace_id)
        and (
            panes_svc.is_pane_close_command(text)
            or panes_svc.is_pane_update_command(text)
        )
    )
    attend_only = (
        emb_svc.enabled()
        and not research
        and not linear
        and not screen_cmd
        and not crud_cmd
        and emb_svc.is_attend_only_command(text)
        and not command.get("_skip_attend")
    )
    # TEMP-LOG: legacy dispatch trace. Remove once pane CRUD is hiccup-free.
    logger.info(
        "[voice %s] legacy dispatch ws=%s research=%s linear=%s screen=%s crud=%s attend=%s text=%r",
        str(command.get("turn_id") or "?"),
        workspace_id,
        research,
        linear,
        screen_cmd,
        crud_cmd,
        attend_only,
        (text or "")[:160],
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

    if screen_cmd:
        opened = panes_svc.open_or_create_from_utterance(workspace_id, uid, text)
        if not opened:
            job["status"] = "failed"
            job["error"] = "Could not open or create a screen from that request."
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
        from app.services.voice_actions import open_screen_result

        ids = [str(p.get("id") or "") for p in opened if p.get("id")]
        command["focused_pane_ids"] = ids
        command["primary_pane_id"] = ids[0]
        job["route"] = str(opened[0].get("route") or job.get("route") or "/")
        job["result_json"] = json.dumps(open_screen_result(opened))
        job["preview_url"] = ""
        job = await attach_embodiment_plan(uid, job, command)
        job["status"] = "ready"
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

    if crud_cmd:
        from app.services.voice_actions import close_pane_result, update_pane_result

        focused = [str(p) for p in (command.get("focused_pane_ids") or []) if str(p)]
        primary = str(command.get("primary_pane_id") or "")
        if panes_svc.is_pane_close_command(text):
            target = panes_svc.resolve_pane_target(
                workspace_id, text, focused=focused, primary=primary
            )
            if target is None:
                job["status"] = "failed"
                job["error"] = "No matching pane to close."
                job["updated_at"] = now_iso()
                put_doc("jobs", job["id"], job)
                return job_view(job)
            closed = panes_svc.delete_pane(workspace_id, str(target.get("id") or ""), uid)
            logger.info(
                "[voice %s] legacy close pane_id=%s ok=%s",
                str(command.get("turn_id") or "?"),
                target.get("id"),
                closed.get("ok"),
            )
            job["result_json"] = json.dumps(
                close_pane_result(
                    str(target.get("id") or ""), str(target.get("title") or "")
                )
            )
            job["affected_pane_ids"] = [str(target.get("id") or "")]
        else:
            parsed = panes_svc.parse_pane_update(text)
            target = panes_svc.resolve_pane_target(
                workspace_id, text, focused=focused, primary=primary
            )
            if target is None:
                job["status"] = "failed"
                job["error"] = "No matching pane to update."
                job["updated_at"] = now_iso()
                put_doc("jobs", job["id"], job)
                return job_view(job)
            view = panes_svc.update_pane(
                workspace_id,
                str(target.get("id") or ""),
                uid,
                title=str(parsed.get("title") or ""),
                route=str(parsed.get("route") or ""),
                kind=str(parsed.get("kind") or ""),
                source_kind=str(parsed.get("source_kind") or ""),
                external_url=str(parsed.get("external_url") or ""),
            )
            if view is None:
                job["status"] = "failed"
                job["error"] = "No matching pane to update."
                job["updated_at"] = now_iso()
                put_doc("jobs", job["id"], job)
                return job_view(job)
            logger.info(
                "[voice %s] legacy update pane_id=%s title=%r route=%r",
                str(command.get("turn_id") or "?"),
                view.get("id"),
                view.get("title"),
                view.get("route"),
            )
            job["result_json"] = json.dumps(update_pane_result(view))
            job["affected_pane_ids"] = [str(view.get("id") or "")]
            job["route"] = str(view.get("route") or job.get("route") or "/")
        job["preview_url"] = ""
        job = await attach_embodiment_plan(uid, job, command)
        job["status"] = "ready"
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

    job = await attach_embodiment_plan(uid, job, command)

    if attend_only:
        plan = _raw_plan(job)
        if plan:
            job["status"] = "completed"
            job["result_json"] = json.dumps(
                {
                    "ok": True,
                    "action": "attend",
                    "summary": "Moving to the focused screen.",
                    "plan_id": plan.get("plan_id") or "",
                }
            )
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
        job["status"] = "failed"
        job["error"] = (
            "No preview pane to walk to. Select a screen, then ask again."
        )
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

    coding_like = not research and not linear
    if coding_like and workspace_id:
        from fastapi import HTTPException

        from app.services.captures import pair_captures_to_targets
        from app.services.coding_targets import resolve_coding_targets

        try:
            snap = await resolve_coding_targets(workspace_id, uid, command)
        except HTTPException as exc:
            detail = exc.detail if isinstance(exc.detail, dict) else {"error": str(exc.detail)}
            code = str(detail.get("error") or "NO_CODING_TARGETS")
            job["mutation_error_code"] = code
            job["error"] = str(detail.get("message") or code)
            job["status"] = "failed"
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
        job["coding_targets"] = snap
        job["base_commit_sha"] = str(snap.get("base_commit_sha") or "")
        paired, reasons = pair_captures_to_targets(
            uid=uid,
            workspace_id=workspace_id,
            targets=list(snap.get("targets") or []),
            captures=[c for c in (command.get("captures") or []) if isinstance(c, dict)],
            context_revision=int(snap.get("context_revision") or 0),
        )
        job["captures"] = paired
        job["capture_audit"] = reasons
        job["updated_at"] = now_iso()
        put_doc("jobs", job["id"], job)

    job = await execute_queued_job(job, uid)

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
    context_revision: int = 0,
    primary_pane_id: str = "",
    focused_pane_ids: list[str] | None = None,
    captures: list[dict[str, Any]] | None = None,
    turn_id: str = "",
    is_final: bool = True,
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
            "context_revision": context_revision,
            "primary_pane_id": primary_pane_id,
            "focused_pane_ids": list(focused_pane_ids or []),
            "captures": list(captures or []),
            "turn_id": turn_id,
            "is_final": is_final,
        },
    )
