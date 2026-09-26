"""Commands, jobs, SSE events, vocalbridge/query, /api/models."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Annotated, Any, AsyncIterator

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse

from app.deps import current_user
from app.firebase_auth import VerifiedUser
from app.schemas import CommandBody, PaneCommandBody, VocalBridgeQuery
from app.services import dialogue as dialogue_svc
from app.services import embodiment as emb_svc
from app.services import jobs as jobs_svc
from app.services import panes as panes_svc
from app.services.models_catalog import list_openrouter_models
from app.agent_kv import now_iso

logger = logging.getLogger("vektral.voice")

router = APIRouter(tags=["jobs"])

_SSE_HEADERS = {
    "Cache-Control": "no-cache, no-transform",
    "X-Accel-Buffering": "no",
    "Connection": "keep-alive",
}


def _ping_event(workspace_id: str) -> str:
    payload = {
        "type": "ping",
        "workspace_id": workspace_id,
        "message": "ping",
        "payload_json": "{}",
        "created_at": now_iso(),
    }
    return f"data: {json.dumps(payload)}\n\n"


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
            "context_revision": int(c.get("context_revision") or 0),
            "primary_pane_id": str(c.get("primary_pane_id") or ""),
            "focused_pane_ids": [
                str(p) for p in (c.get("focused_pane_ids") or []) if str(p)
            ],
            "captures": [
                dict(x) for x in (c.get("captures") or []) if isinstance(x, dict)
            ],
            "turn_id": str(c.get("turn_id") or c.get("turnId") or ""),
            "is_final": bool(c.get("is_final", True)),
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
        "context_revision": body.context_revision,
        "primary_pane_id": body.primary_pane_id,
        "focused_pane_ids": list(body.focused_pane_ids or []),
        "captures": [c.model_dump() for c in (body.captures or [])],
        "turn_id": body.turn_id,
        "is_final": body.is_final,
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
        context_revision=body.context_revision,
        primary_pane_id=body.primary_pane_id,
        focused_pane_ids=list(body.focused_pane_ids or []),
        captures=[c.model_dump() for c in (body.captures or [])],
        turn_id=body.turn_id,
        is_final=body.is_final,
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
            "base_commit_sha": job.get("base_commit_sha"),
            "preview_url": "",
            "reload_version": job.get("reload_version"),
            "coding_targets": job.get("coding_targets") or {},
            "affected_pane_ids": job.get("affected_pane_ids") or [],
            "mutation_error_code": job.get("mutation_error_code") or "",
            "vision_status": job.get("vision_status") or "",
            "error": job.get("error"),
            "logs_tail": (job.get("logs") or "")[-2000:],
            "embodiment_json": job.get("embodiment_json") or {},
        }
    )


def _plan_from_job(job: dict[str, Any]) -> dict[str, Any]:
    plan = job.get("embodiment_json") or {}
    return plan if isinstance(plan, dict) else {}


def _embodiment_sse(
    event_type: str,
    *,
    workspace_id: str,
    plan: dict[str, Any],
    job_id: str = "",
) -> dict[str, Any]:
    return {
        "type": event_type,
        "workspace_id": workspace_id,
        "job_id": job_id or plan.get("job_id") or "",
        "message": (plan.get("intent") or {}).get("type") or event_type,
        "payload_json": json.dumps(plan),
        "created_at": now_iso(),
    }


@router.get("/api/jobs/{job_id}/events")
async def api_job_events(
    job_id: str,
    user: Annotated[VerifiedUser, Depends(current_user)],
) -> StreamingResponse:
    uid = user.uid

    async def event_gen() -> AsyncIterator[str]:
        last_status = ""
        last_reload = -1
        last_plan_sig = ""
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
            plan = _plan_from_job(view)
            plan_sig = f"{plan.get('plan_id')}:{plan.get('revision')}:{plan.get('cancelled')}"
            if plan and plan_sig != last_plan_sig:
                last_plan_sig = plan_sig
                event_type = (
                    "embodiment_cancel" if plan.get("cancelled") else "embodiment_plan"
                )
                yield f"data: {json.dumps(_embodiment_sse(event_type, workspace_id=str(view.get('workspace_id') or ''), plan=plan, job_id=job_id))}\n\n"
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

    return StreamingResponse(
        event_gen(), media_type="text/event-stream", headers=_SSE_HEADERS
    )


@router.get("/api/workspaces/{workspace_id}/events")
async def api_workspace_events(
    workspace_id: str,
    user: Annotated[VerifiedUser, Depends(current_user)],
) -> StreamingResponse:
    uid = user.uid

    async def event_gen() -> AsyncIterator[str]:
        last_sig = ""
        last_emb = ""
        last_turn = ""
        last_play: str | None = None
        silent_s = 0
        for _ in range(180):
            emitted = False
            # Firestore reads are blocking; keep them off the event loop so one
            # stream cannot stall every other request on this single worker.
            panes, jobs = await asyncio.gather(
                asyncio.to_thread(panes_svc.list_panes, workspace_id, uid),
                asyncio.to_thread(jobs_svc.list_workspace_jobs, workspace_id),
            )
            sig_parts = [
                f"{p['id']}:{p['reload_version']}:{p['route']}:{p.get('preview_status') or ''}"
                for p in panes
            ]
            for job in jobs:
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
                        "launch_url": "",
                        "source_kind": p.get("source_kind") or "",
                        "external_url": p.get("external_url") or "",
                        "preview_status": p.get("preview_status") or "",
                        "reload_policy": p.get("reload_policy") or "remount",
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
                emitted = True
            state, latest = await asyncio.gather(
                asyncio.to_thread(emb_svc.get_state, workspace_id, uid),
                asyncio.to_thread(dialogue_svc.latest_turn_event, workspace_id, uid),
            )
            plan = state.get("plan") or {}
            emb_sig = f"{plan.get('plan_id')}:{plan.get('revision')}:{plan.get('cancelled')}"
            if plan and emb_sig != last_emb:
                last_emb = emb_sig
                event_type = (
                    "embodiment_cancel" if plan.get("cancelled") else "embodiment_plan"
                )
                yield f"data: {json.dumps(_embodiment_sse(event_type, workspace_id=workspace_id, plan=plan, job_id=str(plan.get('job_id') or '')))}\n\n"
                emitted = True
            if latest:
                turn_sig = f"{latest.get('turn_id')}:{latest.get('revision')}:{latest.get('status')}"
                if turn_sig != last_turn:
                    last_turn = turn_sig
                    turn_event = {
                        "type": "dialogue_turn_update",
                        "workspace_id": workspace_id,
                        "message": str(latest.get("status") or ""),
                        "payload_json": json.dumps(
                            {
                                "turn_id": latest.get("turn_id") or "",
                                "revision": int(latest.get("revision") or 0),
                                "status": latest.get("status") or "",
                                "action_ids": list(latest.get("action_ids") or []),
                                "pane_ids": list(latest.get("pane_ids") or []),
                                "job_ids": list(latest.get("job_ids") or []),
                            }
                        ),
                        "created_at": now_iso(),
                    }
                    yield f"data: {json.dumps(turn_event)}\n\n"
                    emitted = True
            if last_play is None:
                last_play = (
                    await asyncio.to_thread(
                        dialogue_svc.latest_playback_event_id, workspace_id, uid
                    )
                    or ""
                )
            else:
                played = await asyncio.to_thread(
                    dialogue_svc.playback_events_since, workspace_id, uid, last_play
                )
                for ev in played:
                    last_play = str(ev.get("event_id") or last_play)
                    play_event = {
                        "type": str(ev.get("type") or "playback_started"),
                        "workspace_id": workspace_id,
                        "message": str(ev.get("turn_id") or ""),
                        "payload_json": json.dumps(
                            {
                                "event_id": ev.get("event_id") or "",
                                "turn_id": ev.get("turn_id") or "",
                                "type": ev.get("type") or "",
                            }
                        ),
                        "created_at": ev.get("created_at") or now_iso(),
                    }
                    yield f"data: {json.dumps(play_event)}\n\n"
                    emitted = True
            if emitted:
                silent_s = 0
            else:
                silent_s += 2
                if silent_s >= 15:
                    yield _ping_event(workspace_id)
                    silent_s = 0
            await asyncio.sleep(2.0)
        yield f"data: {json.dumps({'type': 'timeout', 'workspace_id': workspace_id, 'message': 'event stream timed out', 'created_at': now_iso()})}\n\n"

    return StreamingResponse(
        event_gen(), media_type="text/event-stream", headers=_SSE_HEADERS
    )


@router.post("/api/vocalbridge/query")
async def api_vocalbridge_query(
    body: VocalBridgeQuery,
    user: Annotated[VerifiedUser, Depends(current_user)],
) -> dict[str, Any]:
    # TEMP-LOG: full voice ingress trace. Remove once pane CRUD is hiccup-free.
    raw_text = (body.text or body.transcript or "").strip()
    raw_turn = str(body.turn_id or body.turnId or "").strip()
    logger.info(
        "[voice %s] ingress uid=%s ws=%s text_len=%d is_final=%s",
        raw_turn or "<no-turn>",
        user.uid,
        body.workspace_id,
        len(raw_text),
        body.is_final,
    )
    if body.is_final is False:
        logger.info("[voice %s] reject partial_utterance", raw_turn or "<no-turn>")
        raise HTTPException(
            status_code=400,
            detail={
                "error": "partial_utterance",
                "message": "only finalized turns are accepted",
            },
        )
    from app.services.voice_actions import normalize_turn_id

    require_turn = (
        dialogue_svc.orchestration_enabled()
        and dialogue_svc.rollout_stage() != dialogue_svc.STAGE_SHADOW
    )
    turn_id, turn_error = normalize_turn_id(
        str(body.turn_id or body.turnId or ""),
        required=require_turn,
    )
    if turn_error:
        logger.info("[voice %s] reject %s", raw_turn or "<no-turn>", turn_error.get("error"))
        raise HTTPException(status_code=400, detail=turn_error)
    prompt = (body.text or body.transcript or "").strip()
    if not prompt:
        logger.info("[voice %s] reject empty_utterance", turn_id or "<no-turn>")
        raise HTTPException(
            status_code=400,
            detail={
                "error": "empty_utterance",
                "message": "text or transcript is required",
            },
        )
    logger.info("[voice %s] dispatch text=%r", turn_id, prompt[:160])
    view = await jobs_svc.submit_command(
        user.uid,
        {
            "type": "voice",
            "text": prompt,
            "workspace_id": body.workspace_id,
            "pane_id": body.pane_id,
            "route": body.route,
            "model": body.model,
            "new_conversation": body.new_conversation,
            "context_revision": body.context_revision,
            "primary_pane_id": body.primary_pane_id,
            "focused_pane_ids": list(body.focused_pane_ids or []),
            "captures": [c.model_dump() for c in (body.captures or [])],
            "turn_id": turn_id,
            "is_final": True,
        },
    )
    try:
        logger.info(
            "[voice %s] done status=%s duplicate=%s result=%s",
            turn_id,
            view.get("status"),
            view.get("duplicate"),
            str(view.get("result_json") or "")[:220],
        )
    except Exception:  # noqa: BLE001
        pass
    return view
