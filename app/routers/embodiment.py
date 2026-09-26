"""Workspace embodiment context, bootstrap state, and APEX receipts."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException

from app.deps import current_user
from app.firebase_auth import VerifiedUser
from app.schemas import AgentContextSnapshot, EmbodimentReceipt
from app.services import embodiment as emb_svc

router = APIRouter(tags=["embodiment"])


@router.put("/api/workspaces/{workspace_id}/agent-context")
async def api_put_agent_context(
    workspace_id: str,
    body: AgentContextSnapshot,
    user: Annotated[VerifiedUser, Depends(current_user)],
) -> dict[str, Any]:
    return emb_svc.put_context(workspace_id, user.uid, body)


@router.get("/api/workspaces/{workspace_id}/embodiment/state")
async def api_get_embodiment_state(
    workspace_id: str,
    user: Annotated[VerifiedUser, Depends(current_user)],
) -> dict[str, Any]:
    return emb_svc.get_state(workspace_id, user.uid)


@router.post("/api/workspaces/{workspace_id}/embodiment/receipts")
async def api_post_embodiment_receipt(
    workspace_id: str,
    body: EmbodimentReceipt,
    user: Annotated[VerifiedUser, Depends(current_user)],
) -> dict[str, Any]:
    return emb_svc.post_receipt(workspace_id, user.uid, body)


@router.post("/api/workspaces/{workspace_id}/embodiment/cancel")
async def api_cancel_embodiment_plan(
    workspace_id: str,
    user: Annotated[VerifiedUser, Depends(current_user)],
) -> dict[str, Any]:
    plan = emb_svc.cancel_current_plan(workspace_id, user.uid, reason="explicit_cancel")
    if plan is None:
        raise HTTPException(
            status_code=404,
            detail={"error": "not_found", "message": "no embodiment plan"},
        )
    return {"ok": True, "plan": plan}
