"""Organizations — create/list/get + invites, join codes, members, org workspaces."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException

from app.deps import current_user
from app.firebase_auth import VerifiedUser
from app.schemas import (
    InviteCreate,
    InviteView,
    JoinByCode,
    MemberPatch,
    MemberView,
    MutationResult,
    OrgCreate,
    OrgView,
    WorkspaceCreate,
    WorkspaceView,
)
from app.services import orgs as org_svc
from app.services import preview as preview_svc
from app.services import workspaces as ws_svc
from app.store import get_store

router = APIRouter(prefix="/api/orgs", tags=["orgs"])


@router.get("", response_model=list[OrgView])
async def list_orgs(
    user: Annotated[VerifiedUser, Depends(current_user)],
) -> list[dict]:
    return get_store().list_orgs(user.uid)


@router.post("", response_model=OrgView, status_code=201)
async def create_org(
    body: OrgCreate,
    user: Annotated[VerifiedUser, Depends(current_user)],
) -> dict:
    return get_store().create_org(
        user.uid,
        body.name,
        body.slug,
        email=user.email,
        display_name=user.name,
        avatar_url=user.picture,
    )


@router.get("/invites/pending", response_model=list[InviteView])
async def list_my_pending_invites(
    user: Annotated[VerifiedUser, Depends(current_user)],
) -> list[dict]:
    return org_svc.list_my_pending_invites(user.uid, user.email)


@router.post("/join", response_model=MutationResult)
async def join_organization_by_code(
    body: JoinByCode,
    user: Annotated[VerifiedUser, Depends(current_user)],
) -> dict:
    result = org_svc.join_by_code(
        user.uid,
        user.email,
        user.name or user.uid,
        body.code,
        avatar_url=user.picture,
    )
    return {
        "ok": True,
        "message": result.get("message") or "joined",
        "id": result.get("id") or "",
        "join_code": "",
    }


@router.post("/invites/{invite_id}/accept", response_model=MutationResult)
async def accept_organization_invite(
    invite_id: str,
    user: Annotated[VerifiedUser, Depends(current_user)],
) -> dict:
    result = org_svc.accept_invite(
        user.uid,
        user.email,
        user.name or user.uid,
        invite_id,
        avatar_url=user.picture,
    )
    return {
        "ok": True,
        "message": result.get("message") or "accepted",
        "id": result.get("id") or "",
        "join_code": "",
    }


@router.get("/{org_id}", response_model=OrgView)
async def get_org(
    org_id: str,
    user: Annotated[VerifiedUser, Depends(current_user)],
) -> dict:
    return org_svc.get_org_view(user.uid, org_id)


@router.get("/{org_id}/members", response_model=list[MemberView])
async def list_members(
    org_id: str,
    user: Annotated[VerifiedUser, Depends(current_user)],
) -> list[dict]:
    return org_svc.list_members(user.uid, org_id)


@router.patch("/{org_id}/members/{member_uid}", response_model=MemberView)
async def patch_member(
    org_id: str,
    member_uid: str,
    body: MemberPatch,
    user: Annotated[VerifiedUser, Depends(current_user)],
) -> dict:
    return org_svc.update_member_role(user.uid, org_id, member_uid, body.role)


@router.delete("/{org_id}/members/{member_uid}", response_model=MutationResult)
async def delete_member(
    org_id: str,
    member_uid: str,
    user: Annotated[VerifiedUser, Depends(current_user)],
) -> dict:
    return org_svc.remove_member(user.uid, org_id, member_uid)


@router.post("/{org_id}/invites", response_model=InviteView, status_code=201)
async def invite_to_organization(
    org_id: str,
    body: InviteCreate,
    user: Annotated[VerifiedUser, Depends(current_user)],
) -> dict:
    inv = org_svc.invite(user.uid, org_id, body.email, body.role)
    if inv.get("duplicate"):
        raise HTTPException(
            status_code=409,
            detail={"error": "duplicate", "message": "invite already pending", "id": inv.get("id")},
        )
    return inv


@router.get("/{org_id}/invites", response_model=list[InviteView])
async def list_org_invites(
    org_id: str,
    user: Annotated[VerifiedUser, Depends(current_user)],
) -> list[dict]:
    return org_svc.list_org_invites(user.uid, org_id)


@router.post("/{org_id}/join-code", response_model=MutationResult)
async def generate_org_join_code(
    org_id: str,
    user: Annotated[VerifiedUser, Depends(current_user)],
) -> dict:
    result = org_svc.generate_join_code(user.uid, org_id)
    return {
        "ok": True,
        "message": result.get("message") or "join code ready",
        "id": result.get("id") or org_id,
        "join_code": result.get("join_code") or "",
    }


@router.get("/{org_id}/workspaces", response_model=list[WorkspaceView])
async def list_org_workspaces(
    org_id: str,
    user: Annotated[VerifiedUser, Depends(current_user)],
) -> list[dict[str, Any]]:
    return org_svc.list_org_workspaces(user.uid, org_id)


@router.post("/{org_id}/workspaces", response_model=WorkspaceView, status_code=201)
async def create_org_workspace(
    org_id: str,
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
        org_id=org_id,
        create_github_repo=body.create_github_repo,
        github_repo_private=body.github_repo_private,
    )
    await preview_svc.seed_starter_checkout(ws["id"], user.uid)
    return ws
