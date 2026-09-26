"""Pane CRUD under /api/workspaces/{id}/panes."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query

from app.deps import current_user
from app.firebase_auth import VerifiedUser
from app.schemas import PaneCreate, PaneUpdate
from app.services import panes as panes_svc

router = APIRouter(tags=["panes"])


@router.get("/api/workspaces/{workspace_id}/panes")
async def api_list_panes(
    workspace_id: str,
    user: Annotated[VerifiedUser, Depends(current_user)],
    kind: str | None = Query(default=None),
) -> list[dict[str, Any]]:
    return panes_svc.list_panes(workspace_id, user.uid, kind=kind)


@router.post("/api/workspaces/{workspace_id}/panes")
async def api_create_pane(
    workspace_id: str,
    body: PaneCreate,
    user: Annotated[VerifiedUser, Depends(current_user)],
) -> dict[str, Any]:
    return panes_svc.create_pane(
        workspace_id,
        user.uid,
        title=body.title,
        route=body.route,
        layout_json=body.layout_json,
        kind=body.kind,
        source_kind=body.source_kind,
        external_url=body.external_url,
    )


@router.put("/api/workspaces/{workspace_id}/panes/{pane_id}")
async def api_update_pane(
    workspace_id: str,
    pane_id: str,
    body: PaneUpdate,
    user: Annotated[VerifiedUser, Depends(current_user)],
) -> dict[str, Any]:
    updated = panes_svc.update_pane(
        workspace_id,
        pane_id,
        user.uid,
        title=body.title,
        route=body.route,
        layout_json=body.layout_json,
        kind=body.kind,
        source_kind=body.source_kind,
        external_url=body.external_url,
    )
    if updated is None:
        raise HTTPException(status_code=404, detail={"error": "not_found", "message": "pane not found"})
    return updated


@router.delete("/api/workspaces/{workspace_id}/panes/{pane_id}")
async def api_delete_pane(
    workspace_id: str,
    pane_id: str,
    user: Annotated[VerifiedUser, Depends(current_user)],
) -> dict[str, Any]:
    return panes_svc.delete_pane(workspace_id, pane_id, user.uid)


@router.get("/api/workspaces/{workspace_id}/panes/{pane_id}/messages")
async def api_list_pane_messages(
    workspace_id: str,
    pane_id: str,
    user: Annotated[VerifiedUser, Depends(current_user)],
) -> list[dict[str, Any]]:
    return panes_svc.list_messages(workspace_id, pane_id, user.uid)


@router.post("/api/workspaces/{workspace_id}/panes/{pane_id}/activate")
async def api_activate_chat_pane(
    workspace_id: str,
    pane_id: str,
    user: Annotated[VerifiedUser, Depends(current_user)],
) -> dict[str, Any]:
    return panes_svc.activate_chat_pane(workspace_id, pane_id, user.uid)


@router.post("/api/workspaces/{workspace_id}/panes/{pane_id}/launch")
async def api_launch_pane(
    workspace_id: str,
    pane_id: str,
    user: Annotated[VerifiedUser, Depends(current_user)],
) -> dict[str, Any]:
    from app.services import preview as preview_svc

    return await preview_svc.launch_pane(workspace_id, pane_id, user.uid)
