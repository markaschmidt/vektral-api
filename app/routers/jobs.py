"""Commands, jobs, SSE events, vocalbridge/query, /api/models."""

from __future__ import annotations

import asyncio
import json
from typing import Annotated, Any, AsyncIterator

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse

from app.deps import current_user
from app.firebase_auth import VerifiedUser
from app.schemas import CommandBody, PaneCommandBody, VocalBridgeQuery
from app.services import jobs as jobs_svc
from app.services import panes as panes_svc
from app.services.models_catalog import list_openrouter_models
from app.agent_kv import now_iso

router = APIRouter(tags=["jobs"])


def _command_from_body(body: CommandBody) -> dict[str, Any]:
    if body.command is not None:
        c = body.command
        raw_payload = c.get("payload_json", "")
        if isinstance(raw_payload, str):
            cpayload = raw_payload
        elif raw_payload:
            cpayload = json.dumps(raw_payload)
        else:
            cpayload = ""
        return {
            "type": str(c.get("type", "text") or "text"),
            "text": str(c.get("text", "") or ""),
            "workspace_id": str(c.get("workspace_id", "") or ""),
            "pane_id": str(c.get("pane_id", "") or ""),
            "route": str(c.get("route", "") or ""),
            "model": str(c.get("model", "") or ""),
            "payload_json": cpayload,
            "new_conversation": bool(c.get("new_conversation")),
        }
    return {
        "type": body.type,
        "text": body.text,
        "workspace_id": body.workspace_id,
        "pane_id": body.pane_id,
        "route": body.route,
        "model": body.model,
        "payload_json": body.payload_json,
        "new_conversation": body.new_conversation,
    }


@router.get("/api/models")
async def api_list_models(
    user: Annotated[VerifiedUser, Depends(current_user)],
) -> dict[str, Any]:
    _ = user
    return list_openrouter_models()


@router.post("/api/submit_command")
async def api_submit_command(
    body: CommandBody,
    user: Annotated[VerifiedUser, Depends(current_user)],
) -> dict[str, Any]:
    return await jobs_svc.submit_command(user.uid, _command_from_body(body))


@router.post("/api/panes/{pane_id}/commands")
async def api_pane_command(
    pane_id: str,
    body: PaneCommandBody,
    user: Annotated[VerifiedUser, Depends(current_user)],
) -> dict[str, Any]:
    return await jobs_svc.submit_pane_command(
        user.uid,
        pane_id,
        text=body.text,
        type_=body.type,
        model=body.model,
        payload_json=body.payload_json,
        new_conversation=body.new_conversation,
    )


@router.get("/api/jobs/{job_id}")
async def api_get_job(
    job_id: str,
    user: Annotated[VerifiedUser, Depends(current_user)],
) -> dict[str, Any]:
    view = jobs_svc.get_job(job_id, user.uid)
    if view is None:
        raise HTTPException(status_code=404, detail={"error": "not_found", "message": "job not found"})
    return view


def _job_event_payload(job: dict[str, Any]) -> str:
    return json.dumps(
        {
            "id": job.get("id"),
            "status": job.get("status"),
            "workspace_id": job.get("workspace_id"),
            "pane_id": job.get("pane_id"),
            "route": job.get("route"),
            "branch": job.get("branch"),
            "commit_sha": job.get("commit_sha"),
            "preview_url": job.get("preview_url"),
            "reload_version": job.get("reload_version"),
            "error": job.get("error"),
            "logs_tail": (job.get("logs") or "")[-2000:],
        }
    )


@router.get("/api/jobs/{job_id}/events")
async def api_job_events(
    job_id: str,
    user: Annotated[VerifiedUser, Depends(current_user)],
) -> StreamingResponse:
    uid = user.uid

    async def event_gen() -> AsyncIterator[str]:
        last_status = ""
        last_reload = -1
        for _ in range(120):
            view = jobs_svc.get_job(job_id, uid)
            if view is None:
                payload = {
                    "type": "error",
                    "job_id": job_id,
                    "message": "job not found",
                    "created_at": now_iso(),
                }
                yield f"data: {json.dumps(payload)}\n\n"
                return
            status = str(view.get("status") or "")
            reload_v = int(view.get("reload_version") or 0)
            if status != last_status or reload_v != last_reload:
                last_status = status
                last_reload = reload_v
                payload = {
                    "type": "job_update",
                    "job_id": view.get("id"),
                    "workspace_id": view.get("workspace_id"),
                    "pane_id": view.get("pane_id"),
                    "message": status,
                    "payload_json": _job_event_payload(view),
                    "created_at": now_iso(),
                }
                yield f"data: {json.dumps(payload)}\n\n"
                if status in ("ready", "failed", "completed"):
                    terminal = {
                        "type": "pane_reload" if status in ("ready", "completed") else "job_failed",
                        "job_id": view.get("id"),
                        "workspace_id": view.get("workspace_id"),
                        "pane_id": view.get("pane_id"),
                        "message": view.get("preview_url")
                        if status in ("ready", "completed")
                        else view.get("error"),
                        "payload_json": _job_event_payload(view),
                        "created_at": now_iso(),
                    }
                    yield f"data: {json.dumps(terminal)}\n\n"
                    return
            await asyncio.sleep(1.0)
        yield f"data: {json.dumps({'type': 'timeout', 'job_id': job_id, 'message': 'event stream timed out', 'created_at': now_iso()})}\n\n"

    return StreamingResponse(event_gen(), media_type="text/event-stream")


@router.get("/api/workspaces/{workspace_id}/events")
async def api_workspace_events(
    workspace_id: str,
    user: Annotated[VerifiedUser, Depends(current_user)],
) -> StreamingResponse:
    uid = user.uid

    async def event_gen() -> AsyncIterator[str]:
        last_sig = ""
        for _ in range(180):
            panes = panes_svc.list_panes(workspace_id, uid)
            sig_parts = [f"{p['id']}:{p['reload_version']}:{p['route']}" for p in panes]
            for job in jobs_svc.list_workspace_jobs(workspace_id):
                if job.get("owner_uid") and job.get("owner_uid") != uid:
                    continue
                sig_parts.append(
                    f"job:{job.get('id')}:{job.get('status')}:{job.get('reload_version')}"
                )
            sig = "|".join(sig_parts)
            if sig != last_sig:
                last_sig = sig
                pane_payload = [
                    {
                        "id": p["id"],
                        "route": p["route"],
                        "kind": p.get("kind") or "preview",
                        "preview_url": p["preview_url"],
                        "reload_version": p["reload_version"],
                        "title": p["title"],
                    }
                    for p in panes
                ]
                payload = {
                    "type": "workspace_update",
                    "workspace_id": workspace_id,
                    "message": f"{len(panes)} panes",
                    "payload_json": json.dumps({"panes": pane_payload}),
                    "created_at": now_iso(),
                }
                yield f"data: {json.dumps(payload)}\n\n"
            await asyncio.sleep(2.0)
        yield f"data: {json.dumps({'type': 'timeout', 'workspace_id': workspace_id, 'message': 'event stream timed out', 'created_at': now_iso()})}\n\n"

    return StreamingResponse(event_gen(), media_type="text/event-stream")


@router.post("/api/vocalbridge/query")
async def api_vocalbridge_query(
    body: VocalBridgeQuery,
    user: Annotated[VerifiedUser, Depends(current_user)],
) -> dict[str, Any]:
    prompt = body.text if body.text else body.transcript
    return await jobs_svc.submit_command(
        user.uid,
        {
            "type": "voice",
            "text": prompt,
            "workspace_id": body.workspace_id,
            "pane_id": body.pane_id,
            "route": body.route,
            "model": body.model,
            "new_conversation": body.new_conversation,
        },
    )
