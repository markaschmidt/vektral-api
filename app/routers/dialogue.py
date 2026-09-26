"""Dialogue turn API — finalized user/assistant transcripts."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException

from app.deps import current_user
from app.firebase_auth import VerifiedUser
from app.schemas import DialogueTurnRequestV1, PlaybackEventRequest
from app.services import dialogue as dialogue_svc

router = APIRouter(tags=["dialogue"])


@router.post("/api/workspaces/{workspace_id}/dialogue/turns")
async def api_post_dialogue_turn(
    workspace_id: str,
    body: DialogueTurnRequestV1,
    user: Annotated[VerifiedUser, Depends(current_user)],
) -> dict[str, Any]:
    if not dialogue_svc.orchestration_enabled():
        raise HTTPException(
            status_code=404,
            detail={
                "error": "disabled",
                "message": "dialogue orchestration is not enabled",
            },
        )
    return await dialogue_svc.submit_turn(user.uid, workspace_id, body)


@router.get("/api/workspaces/{workspace_id}/dialogue/turns/{turn_id}")
async def api_get_dialogue_turn(
    workspace_id: str,
    turn_id: str,
    user: Annotated[VerifiedUser, Depends(current_user)],
) -> dict[str, Any]:
    if not dialogue_svc.orchestration_enabled():
        raise HTTPException(
            status_code=404,
            detail={
                "error": "disabled",
                "message": "dialogue orchestration is not enabled",
            },
        )
    result = dialogue_svc.get_turn(workspace_id, user.uid, turn_id)
    if result is None:
        raise HTTPException(
            status_code=404,
            detail={"error": "not_found", "message": "turn not found"},
        )
    return result


@router.post("/api/workspaces/{workspace_id}/dialogue/playback-events")
async def api_post_playback_event(
    workspace_id: str,
    body: PlaybackEventRequest,
    user: Annotated[VerifiedUser, Depends(current_user)],
) -> dict[str, Any]:
    if not dialogue_svc.orchestration_enabled():
        raise HTTPException(
            status_code=404,
            detail={
                "error": "disabled",
                "message": "dialogue orchestration is not enabled",
            },
        )
    try:
        return dialogue_svc.record_playback_event(workspace_id, user.uid, body)
    except HTTPException:
        raise
    except Exception:  # noqa: BLE001
        return {
            "ok": False,
            "duplicate": False,
            "event_id": body.event_id,
            "turn_id": body.turn_id,
            "type": body.type,
        }
