"""Personal workspaces — list/create/get/patch/delete."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Response

from app.deps import current_user
from app.firebase_auth import VerifiedUser
from app.schemas import WorkspaceCreate, WorkspacePatch, WorkspaceView, workspace_view
from app.services import preview as preview_svc
from app.services import workspaces as ws_svc
from app.store import get_store

router = APIRouter(prefix="/api/workspaces", tags=["workspaces"])


@router.get("", response_model=list[WorkspaceView])
async def list_workspaces(
    user: Annotated[VerifiedUser, Depends(current_user)],
) -> list[dict[str, Any]]:
    # Metadata only — no GitHub/Linear/preview. Names already stored on create.
    return ws_svc.list_workspaces_for_user(user.uid)


@router.post("", response_model=WorkspaceView, status_code=201)
async def create_workspace(
    body: WorkspaceCreate,
    user: Annotated[VerifiedUser, Depends(current_user)],
) -> dict[str, Any]:
    ws = ws_svc.create_workspace(
        user.uid,
        name=body.name,
        description=body.description,
        repo_full_name=body.repo_full_name or "",
        default_branch=body.default_branch,
        vektral_branch=body.vektral_branch,
        org_id=body.org_id or "",
        create_github_repo=body.create_github_repo,
        github_repo_private=body.github_repo_private,
        linear_project_id=body.linear_project_id or "",
        linear_team_id=body.linear_team_id or "",
    )
    ws = await ws_svc.apply_linear_labels(user.uid, ws)
    await preview_svc.seed_starter_checkout(ws["id"], user.uid)
    return workspace_view(ws_svc.require_workspace_access(ws["id"], user.uid))


@router.get("/{workspace_id}", response_model=WorkspaceView)
async def get_workspace(
    workspace_id: str,
    user: Annotated[VerifiedUser, Depends(current_user)],
) -> dict[str, Any]:
    ws = ws_svc.require_workspace_access(workspace_id, user.uid)
    return workspace_view(await ws_svc.apply_linear_labels(user.uid, ws))


@router.patch("/{workspace_id}", response_model=WorkspaceView)
async def patch_workspace(
    workspace_id: str,
    body: WorkspacePatch,
    user: Annotated[VerifiedUser, Depends(current_user)],
) -> dict[str, Any]:
    previous = ws_svc.get_workspace(workspace_id)
    old_repo = str((previous or {}).get("repo_full_name") or "").strip()
    dump = body.model_dump(exclude_unset=True)
    updated = ws_svc.patch_workspace(workspace_id, user.uid, dump)
    if "linear_project_id" in dump or "linear_team_id" in dump:
        updated = await ws_svc.apply_linear_labels(user.uid, updated)
    new_repo = str(updated.get("repo_full_name") or "").strip()
    if new_repo and new_repo != old_repo:
        await preview_svc.seed_starter_checkout(workspace_id, user.uid)
        updated = workspace_view(
            ws_svc.require_workspace_access(workspace_id, user.uid)
        )
    return updated


@router.delete("/{workspace_id}", status_code=204)
async def delete_workspace(
    workspace_id: str,
    user: Annotated[VerifiedUser, Depends(current_user)],
) -> Response:
    ok = get_store().soft_delete_workspace(user.uid, workspace_id)
    if not ok:
        raise HTTPException(
            status_code=404,
            detail={"error": "not_found", "message": "workspace not found"},
        )
    return Response(status_code=204)
