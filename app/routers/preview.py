"""Preview start/restart/status/logs — orchestrates Vektral-Collab."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends

from app.deps import current_user
from app.firebase_auth import VerifiedUser
from app.services import preview as preview_svc

router = APIRouter(tags=["preview"])


@router.post("/api/workspaces/{workspace_id}/preview/start")
async def api_preview_start(
    workspace_id: str,
    user: Annotated[VerifiedUser, Depends(current_user)],
) -> dict[str, Any]:
    return await preview_svc.start_preview(workspace_id, user.uid)


@router.post("/api/workspaces/{workspace_id}/preview/restart")
async def api_preview_restart(
    workspace_id: str,
    user: Annotated[VerifiedUser, Depends(current_user)],
) -> dict[str, Any]:
    return await preview_svc.restart_preview(workspace_id, user.uid)


@router.get("/api/workspaces/{workspace_id}/preview/status")
async def api_preview_status(
    workspace_id: str,
    user: Annotated[VerifiedUser, Depends(current_user)],
) -> dict[str, Any]:
    return await preview_svc.preview_status(workspace_id, user.uid)


@router.get("/api/workspaces/{workspace_id}/preview/logs")
async def api_preview_logs(
    workspace_id: str,
    user: Annotated[VerifiedUser, Depends(current_user)],
) -> dict[str, Any]:
    return await preview_svc.preview_logs(workspace_id, user.uid)
