"""Workspace domain helpers — backed by DomainStore (Plan B + org ACL)."""

from __future__ import annotations

from typing import Any

from fastapi import HTTPException

from app.schemas import workspace_view
from app.services import github_repos as gh_repos
from app.services import orgs as org_svc
from app.store import get_store, new_id


def branch_name(workspace_id: str) -> str:
    short = workspace_id.replace(":", "-").replace("/", "-")
    if len(short) > 40:
        short = short[:40]
    return f"vektral/{short}"


def list_workspaces_for_user(uid: str) -> list[dict[str, Any]]:
    return get_store().list_workspaces(uid)


def create_workspace(uid: str, name: str = "Untitled", **fields: Any) -> dict[str, Any]:
    org_id = (fields.get("org_id") or "").strip()
    if org_id:
        org_svc.require_capability(uid, org_id, "create_workspace")

    repo_full_name = (fields.get("repo_full_name") or "").strip()
    default_branch = (fields.get("default_branch") or "main").strip() or "main"
    create_github_repo = bool(fields.get("create_github_repo"))
    github_repo_private = bool(fields.get("github_repo_private", True))

    if create_github_repo and not repo_full_name:
        created = gh_repos.create_user_repo(
            uid,
            name=name or "Untitled",
            description=fields.get("description") or "",
            private=github_repo_private,
        )
        repo_full_name = created.get("full_name") or ""
        default_branch = created.get("default_branch") or default_branch

    wid = new_id("ws_")
    return get_store().create_workspace(
        uid,
        {
            "id": wid,
            "name": name or "Untitled",
            "description": fields.get("description") or "",
            "repo_full_name": repo_full_name,
            "default_branch": default_branch,
            "vektral_branch": fields.get("vektral_branch") or branch_name(wid),
            "org_id": org_id,
            "thumbnail_url": fields.get("thumbnail_url"),
            "linear_project_id": (fields.get("linear_project_id") or "").strip(),
            "linear_team_id": (fields.get("linear_team_id") or "").strip(),
        },
    )


def get_workspace(workspace_id: str) -> dict[str, Any] | None:
    return get_store().get_workspace_raw(workspace_id)


def require_workspace_access(workspace_id: str, uid: str) -> dict[str, Any]:
    ws = get_workspace(workspace_id)
    if ws is None:
        raise HTTPException(
            status_code=404,
            detail={"error": "not_found", "message": "workspace not found"},
        )
    if not org_svc.can_access_org_workspace(uid, ws):
        raise HTTPException(
            status_code=403,
            detail={"error": "forbidden", "message": "workspace forbidden"},
        )
    return ws


def require_workspace_mutate(workspace_id: str, uid: str) -> dict[str, Any]:
    ws = require_workspace_access(workspace_id, uid)
    if not org_svc.can_mutate_workspace(uid, ws):
        raise HTTPException(
            status_code=403,
            detail={"error": "forbidden", "message": "workspace mutate forbidden"},
        )
    return ws


def patch_workspace(workspace_id: str, uid: str, patch: dict[str, Any]) -> dict[str, Any]:
    ws = require_workspace_mutate(workspace_id, uid)

    if "name" in patch and patch["name"] is not None:
        new_name = (patch["name"] or "").strip()
        if not new_name:
            raise HTTPException(
                status_code=400,
                detail={"error": "invalid_name", "message": "project name is required"},
            )
        repo_full_name = (ws.get("repo_full_name") or "").strip()
        if repo_full_name:
            token = github_token_for_user(uid)
            verify = gh_repos.verify_repo_access(repo_full_name, token)
            if not verify.get("accessible"):
                raise HTTPException(
                    status_code=409,
                    detail={
                        "error": "repo_inaccessible",
                        "message": (
                            "Cannot rename this project because the linked GitHub "
                            "repository is inaccessible or deleted."
                        ),
                    },
                )
            rename_result = gh_repos.rename_user_repo(uid, repo_full_name, new_name)
            new_full = (rename_result.get("full_name") or "").strip()
            if new_full and new_full != repo_full_name:
                patch["repo_full_name"] = new_full
        patch["name"] = new_name

    if "org_id" in patch and patch["org_id"] is not None:
        new_org = (patch["org_id"] or "").strip()
        if new_org:
            org_svc.require_membership(uid, new_org)
    updated = get_store().update_workspace(uid, workspace_id, patch)
    if updated is None:
        raise HTTPException(
            status_code=404,
            detail={"error": "not_found", "message": "workspace not found"},
        )
    return updated


def enter_workspace(workspace_id: str, uid: str) -> dict[str, Any]:
    return workspace_view(require_workspace_access(workspace_id, uid))


def github_token_for_user(uid: str) -> str:
    """Return stored GitHub token for preview clone (never expose to Web)."""
    token = get_store().github_access_token(uid)
    if token:
        return token
    from app.config import get_settings

    return get_settings().github_token_fallback
