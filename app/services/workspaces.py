"""Workspace domain helpers — backed by DomainStore (Plan B + org ACL)."""

from __future__ import annotations

from typing import Any

from fastapi import HTTPException

from app.schemas import workspace_view
from app.services import github_repos as gh_repos
from app.services import orgs as org_svc
from app.store import get_store, new_id

SITE_READY = "ready"
SITE_SEEDING = "seeding"
SITE_FAILED = "failed"


def _require_github_for_repo(uid: str, *, repo_full_name: str = "") -> str:
    """Fail fast when the user wants a GitHub-backed project but the token is dead."""
    from app.services.oauth_refresh import ensure_github_access_token

    try:
        token = ensure_github_access_token(uid)
    except HTTPException:
        raise
    if not token:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "github_not_connected",
                "message": "Connect GitHub under Integrations before linking a repository.",
            },
        )
    name = (repo_full_name or "").strip()
    if not name:
        return token
    verify = gh_repos.verify_repo_access(name, token)
    if verify.get("accessible"):
        return token
    reason = str(verify.get("reason") or "")
    if reason == "forbidden":
        message = (
            "GitHub access expired or this account cannot open that repository. "
            "Reconnect GitHub under Integrations, then try again."
        )
    elif reason == "not_found":
        message = "GitHub repository was deleted or could not be found."
    else:
        message = "The linked GitHub repository is not accessible with your account."
    raise HTTPException(
        status_code=409,
        detail={"error": "repo_inaccessible", "message": message, "reason": reason},
    )


def site_status_of(ws: dict[str, Any] | None) -> str:
    status = str((ws or {}).get("site_status") or "").strip()
    return status or SITE_READY


def is_site_ready(ws: dict[str, Any] | None) -> bool:
    return site_status_of(ws) == SITE_READY


def require_site_ready(ws: dict[str, Any]) -> None:
    if is_site_ready(ws):
        return
    status = site_status_of(ws)
    err = str(ws.get("site_error") or "").strip()
    if status == SITE_SEEDING:
        message = (
            "The project website is still being committed and merged. "
            "Wait until site_status is ready before loading."
        )
    else:
        message = err or (
            "The project website is not ready on the default branch yet."
        )
    raise HTTPException(
        status_code=409,
        detail={
            "error": "site_not_ready",
            "message": message,
            "site_status": status,
        },
    )


def set_site_status(
    uid: str,
    workspace_id: str,
    *,
    status: str,
    error: str = "",
    commit_sha: str = "",
) -> dict[str, Any] | None:
    return get_store().update_workspace(
        uid,
        workspace_id,
        {
            "site_status": status,
            "site_ready": status == SITE_READY,
            "site_error": error,
            "site_commit_sha": commit_sha,
        },
    )


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

    if create_github_repo or repo_full_name:
        _require_github_for_repo(uid, repo_full_name=repo_full_name)

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
            "linear_project_name": (fields.get("linear_project_name") or "").strip(),
            "linear_project_url": (fields.get("linear_project_url") or "").strip(),
            "linear_team_name": (fields.get("linear_team_name") or "").strip(),
            "linear_organization_id": (fields.get("linear_organization_id") or "").strip(),
            "linear_organization_name": (fields.get("linear_organization_name") or "").strip(),
            "site_status": SITE_SEEDING if repo_full_name else SITE_READY,
            "site_ready": not bool(repo_full_name),
            "site_error": "",
            "site_commit_sha": "",
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

    if "repo_full_name" in patch and (patch.get("repo_full_name") or "").strip():
        _require_github_for_repo(uid, repo_full_name=str(patch["repo_full_name"]).strip())

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
    if "linear_project_id" in patch or "linear_team_id" in patch:
        from app.services.linear_client import empty_linear_labels

        pid = (
            patch["linear_project_id"]
            if "linear_project_id" in patch
            else ws.get("linear_project_id")
        )
        tid = (
            patch["linear_team_id"]
            if "linear_team_id" in patch
            else ws.get("linear_team_id")
        )
        if not str(pid or "").strip() and not str(tid or "").strip():
            patch.update(empty_linear_labels())
    updated = get_store().update_workspace(uid, workspace_id, patch)
    if updated is None:
        raise HTTPException(
            status_code=404,
            detail={"error": "not_found", "message": "workspace not found"},
        )
    return updated


def _needs_linear_names(ws: dict[str, Any]) -> bool:
    has_ids = bool(
        (ws.get("linear_project_id") or "") or (ws.get("linear_team_id") or "")
    )
    has_names = bool(
        (ws.get("linear_project_name") or "") or (ws.get("linear_team_name") or "")
    )
    return has_ids and not has_names


async def resolve_linear_labels(
    uid: str, project_id: str = "", team_id: str = ""
) -> dict[str, str]:
    from app.services.linear_client import empty_linear_labels, labels_for_ids

    pid = (project_id or "").strip()
    tid = (team_id or "").strip()
    if not pid and not tid:
        return empty_linear_labels()
    from app.services.oauth_refresh import ensure_linear_access_token

    try:
        token = ensure_linear_access_token(uid)
    except Exception:  # noqa: BLE001
        return empty_linear_labels()
    if not token:
        return empty_linear_labels()
    return await labels_for_ids(token, pid, tid)


async def apply_linear_labels(uid: str, workspace: dict[str, Any]) -> dict[str, Any]:
    labels = await resolve_linear_labels(
        uid,
        str(workspace.get("linear_project_id") or ""),
        str(workspace.get("linear_team_id") or ""),
    )
    if not any(str(v).strip() for v in labels.values()):
        return workspace
    updated = get_store().update_workspace(uid, workspace["id"], labels)
    return updated or {**workspace, **labels}


async def hydrate_linear_labels(
    uid: str, views: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    need = [v for v in views if _needs_linear_names(v)]
    if not need:
        return views
    token = get_store().linear_access_token(uid)
    if not token:
        from app.services.oauth_refresh import ensure_linear_access_token

        try:
            token = ensure_linear_access_token(uid)
        except Exception:  # noqa: BLE001
            return views
    if not token:
        return views
    from app.services.linear_client import labels_from_catalog, list_projects, list_teams

    try:
        projects = await list_projects(token)
        teams = await list_teams(token)
    except Exception:  # noqa: BLE001
        return views
    out: list[dict[str, Any]] = []
    for view in views:
        if not _needs_linear_names(view):
            out.append(view)
            continue
        labels = labels_from_catalog(
            projects,
            teams,
            str(view.get("linear_project_id") or ""),
            str(view.get("linear_team_id") or ""),
        )
        if not any(str(v).strip() for v in labels.values()):
            out.append(view)
            continue
        updated = get_store().update_workspace(uid, view["id"], labels)
        out.append(updated or {**view, **labels})
    return out


def enter_workspace(workspace_id: str, uid: str) -> dict[str, Any]:
    ws = require_workspace_access(workspace_id, uid)
    require_site_ready(ws)
    return workspace_view(ws)


def github_token_for_user(uid: str) -> str:
    """Return stored GitHub token for preview clone (never expose to Web)."""
    from fastapi import HTTPException

    from app.config import get_settings
    from app.services.oauth_refresh import ensure_github_access_token

    try:
        token = ensure_github_access_token(uid)
    except HTTPException:
        token = ""
    if token:
        return token
    return get_settings().github_token_fallback
