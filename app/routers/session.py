"""VR / Web session bootstrap + workspace enter (CRUD lives on /api/workspaces)."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends

from app.config import get_settings
from app.deps import current_profile, current_user
from app.firebase_auth import VerifiedUser
from app.services import panes as panes_svc
from app.services import workspaces as ws_svc
from app.store import get_store

router = APIRouter(tags=["session"])


def _session_payload(profile: dict[str, Any], uid: str) -> dict[str, Any]:
    store = get_store()
    gh = store.github_status(uid)
    lin = store.linear_status(uid)
    settings = get_settings()
    orgs = store.list_orgs(uid)
    workspaces = store.list_workspaces(uid)
    if gh.get("connected"):
        profile = {
            **profile,
            "github_connected": True,
            "github_login": gh.get("github_login") or profile.get("github_login") or "",
        }
    return {
        "profile": {
            "id": profile.get("id") or profile.get("uid") or uid,
            "display_name": profile.get("display_name") or "",
            "email": profile.get("email") or "",
            "avatar_url": profile.get("avatar_url") or profile.get("photo_url") or "",
            "photo_url": profile.get("photo_url") or profile.get("avatar_url") or "",
            "github_login": profile.get("github_login") or "",
            "github_connected": bool(profile.get("github_connected")),
            "root_id": profile.get("uid") or uid,
            "is_independent": True,
            "org_count": len(orgs),
        },
        "github": {
            "connected": bool(gh.get("connected")),
            "github_login": gh.get("github_login") or "",
            "connected_at": gh.get("connected_at") or "",
            "configured": bool(settings.github_client_id and settings.github_client_secret),
        },
        "linear": {
            "connected": bool(lin.get("connected")),
            "linear_name": lin.get("linear_name") or "",
            "linear_email": lin.get("linear_email") or "",
            "connected_at": lin.get("connected_at") or "",
            "configured": bool(settings.linear_client_id and settings.linear_client_secret),
            "default_organization_id": lin.get("default_organization_id") or "",
            "default_team_id": lin.get("default_team_id") or "",
        },
        "orgs": orgs,
        "workspaces": workspaces,
        "panes": panes_svc.session_pane_slice(uid, workspaces),
        "_meta": {
            "stub": False,
            "wave": 3,
            "message": "Session from domain store + Wave 3 agent routes (no Jac)",
            "runtime": "fastapi",
            "jac": False,
        },
    }


@router.get("/api/session")
@router.post("/api/session")
async def api_session(
    profile: Annotated[dict[str, Any], Depends(current_profile)],
    user: Annotated[VerifiedUser, Depends(current_user)],
) -> dict[str, Any]:
    return _session_payload(profile, user.uid)


@router.post("/api/workspaces/{workspace_id}/enter")
async def api_enter_workspace(
    workspace_id: str,
    user: Annotated[VerifiedUser, Depends(current_user)],
) -> dict[str, Any]:
    return ws_svc.enter_workspace(workspace_id, user.uid)
