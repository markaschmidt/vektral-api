"""Shared Pydantic models + dict view helpers for Vektral-API."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


# --- Plan C agent bodies ---


class PaneCreate(BaseModel):
    title: str = ""
    route: str = "/"
    layout_json: str = ""
    kind: str = ""


class PaneUpdate(BaseModel):
    title: str = ""
    route: str = ""
    layout_json: str = ""
    kind: str = ""


class CommandBody(BaseModel):
    type: str = "text"
    text: str = ""
    workspace_id: str = ""
    pane_id: str = ""
    route: str = ""
    model: str = ""
    payload_json: str = ""
    new_conversation: bool = False
    command: dict[str, Any] | None = None


class PaneCommandBody(BaseModel):
    text: str = ""
    type: str = "text"
    model: str = ""
    payload_json: str = ""
    new_conversation: bool = False


class VocalBridgeQuery(BaseModel):
    text: str = ""
    transcript: str = ""
    workspace_id: str = ""
    pane_id: str = ""
    route: str = ""
    model: str = ""
    new_conversation: bool = False


class FirecrawlScrapeBody(BaseModel):
    url: str
    formats: list[str] = Field(default_factory=list)
    only_main_content: bool = True
    workspace_id: str = ""


class FirecrawlCrawlBody(BaseModel):
    url: str
    workspace_id: str = ""
    limit: int = 10


# --- Plan B domain ---


class WorkspaceCreate(BaseModel):
    name: str = Field(default="Untitled", min_length=1, max_length=120)
    description: str = ""
    repo_full_name: str | None = None
    default_branch: str = "main"
    vektral_branch: str = ""
    org_id: str | None = None
    create_github_repo: bool = False
    github_repo_private: bool = True
    linear_project_id: str | None = None
    linear_team_id: str | None = None


class WorkspacePatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    description: str | None = None
    repo_full_name: str | None = None
    default_branch: str | None = None
    vektral_branch: str | None = None
    thumbnail_url: str | None = None
    org_id: str | None = None
    linear_project_id: str | None = None
    linear_team_id: str | None = None


class WorkspaceView(BaseModel):
    id: str
    name: str
    description: str = ""
    slug: str = ""
    repo_full_name: str | None = None
    default_branch: str = "main"
    vektral_branch: str = ""
    storage_prefix: str = ""
    org_id: str | None = None
    is_personal: bool = True
    thumbnail_url: str | None = None
    owner_uid: str = ""
    linear_project_id: str | None = None
    linear_team_id: str | None = None
    created_at: str = ""
    updated_at: str = ""


class OrgCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    slug: str | None = Field(default=None, max_length=80)


class OrgView(BaseModel):
    id: str
    name: str
    slug: str = ""
    my_role: str = "owner"
    my_capabilities: list[str] = Field(default_factory=list)
    member_count: int = 1
    owner_uid: str = ""
    has_join_code: bool = False
    join_code: str = ""
    created_at: str = ""


class MemberView(BaseModel):
    uid: str
    email: str = ""
    display_name: str = ""
    avatar_url: str = ""
    role: str = "member"
    status: str = "active"
    joined_at: str = ""


class MemberPatch(BaseModel):
    role: str = Field(min_length=1, max_length=32)


class InviteCreate(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    role: str = "member"


class InviteView(BaseModel):
    id: str
    email: str
    role: str = "member"
    status: str = "pending"
    org_id: str = ""
    org_name: str = ""
    created_at: str = ""


class JoinByCode(BaseModel):
    code: str = Field(min_length=1, max_length=32)


class MutationResult(BaseModel):
    ok: bool = True
    message: str = ""
    id: str = ""
    join_code: str = ""


class GithubStatus(BaseModel):
    connected: bool = False
    github_login: str = ""
    connected_at: str = ""
    configured: bool = False


class GithubConnectStart(BaseModel):
    url: str = ""
    state: str = ""
    message: str = ""


class GithubRepoView(BaseModel):
    full_name: str
    html_url: str = ""
    default_branch: str = "main"
    private: bool = False
    description: str = ""
    pushed_at: str = ""
    updated_at: str = ""
    language: str = ""
    stargazers_count: int = 0


class GithubRepoVerifyRequest(BaseModel):
    repos: list[str] = Field(default_factory=list, max_length=50)


class GithubRepoVerifyResult(BaseModel):
    full_name: str
    accessible: bool
    reason: str = ""


class LinearStatus(BaseModel):
    connected: bool = False
    linear_user_id: str = ""
    linear_name: str = ""
    linear_email: str = ""
    connected_at: str = ""
    configured: bool = False
    default_organization_id: str = ""
    default_team_id: str = ""


class LinearConnectStart(BaseModel):
    url: str = ""
    state: str = ""
    message: str = ""


class LinearOrganizationView(BaseModel):
    id: str
    name: str
    url_key: str = ""
    scope: str = "organization"


class LinearTeamView(BaseModel):
    id: str
    name: str
    key: str = ""
    description: str = ""
    private: bool = False
    organization_id: str = ""
    organization_name: str = ""


class LinearProjectTeamView(BaseModel):
    id: str = ""
    name: str = ""
    key: str = ""


class LinearProjectView(BaseModel):
    id: str
    name: str
    description: str = ""
    url: str = ""
    state: str = ""
    team_id: str = ""
    team_name: str = ""
    team_key: str = ""
    teams: list[LinearProjectTeamView] = Field(default_factory=list)


class LinearProjectCreateBody(BaseModel):
    name: str
    description: str = ""
    team_id: str = ""


class LinearDefaultsBody(BaseModel):
    organization_id: str = ""
    team_id: str = ""


def workspace_view(doc: dict[str, Any]) -> dict[str, Any]:
    """Normalize workspace docs for VR + Web (includes description for dashboard)."""
    from app.store import workspace_to_view

    return workspace_to_view(doc)


def pane_view(doc: dict[str, Any], preview_base_url: str = "") -> dict[str, Any]:
    route = doc.get("route") or "/"
    if not str(route).startswith("/"):
        route = f"/{route}"
    kind = str(doc.get("kind") or "preview").strip().lower()
    if kind != "chat":
        kind = "preview"
    reload_v = int(doc.get("reload_version") or 0)
    preview = ""
    if kind != "chat":
        base = (preview_base_url or "").rstrip("/")
        preview = f"{base}{route}?v={reload_v}" if base else ""
    return {
        "id": doc.get("id") or "",
        "workspace_id": doc.get("workspace_id") or "",
        "title": doc.get("title") or "",
        "kind": kind,
        "route": route,
        "preview_url": preview,
        "layout_json": doc.get("layout_json") or "",
        "reload_version": reload_v,
        "created_at": doc.get("created_at") or "",
        "updated_at": doc.get("updated_at") or "",
    }


def message_view(doc: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": doc.get("id") or "",
        "pane_id": doc.get("pane_id") or "",
        "role": doc.get("role") or "user",
        "text": doc.get("text") or "",
        "job_id": doc.get("job_id") or "",
        "created_at": doc.get("created_at") or "",
        "owner_uid": doc.get("owner_uid") or "",
    }


def job_view(doc: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": doc.get("id") or "",
        "status": doc.get("status") or "queued",
        "command_type": doc.get("command_type") or "",
        "command_text": doc.get("command_text") or "",
        "workspace_id": doc.get("workspace_id") or "",
        "pane_id": doc.get("pane_id") or "",
        "route": doc.get("route") or "",
        "model": doc.get("model") or "",
        "branch": doc.get("branch") or "",
        "commit_sha": doc.get("commit_sha") or "",
        "preview_url": doc.get("preview_url") or "",
        "reload_version": int(doc.get("reload_version") or 0),
        "logs": doc.get("logs") or "",
        "result_json": doc.get("result_json") or "",
        "error": doc.get("error") or "",
        "created_at": doc.get("created_at") or "",
        "updated_at": doc.get("updated_at") or "",
        "owner_uid": doc.get("owner_uid") or "",
    }


def preview_view(doc: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": doc.get("id") or doc.get("workspace_id") or "",
        "workspace_id": doc.get("workspace_id") or "",
        "status": doc.get("status") or "stopped",
        "branch": doc.get("branch") or "",
        "preview_base_url": doc.get("preview_base_url") or "",
        "container_id": doc.get("container_id") or "",
        "port": int(doc.get("port") or 0),
        "commit_sha": doc.get("commit_sha") or "",
        "logs": doc.get("logs") or "",
        "error": doc.get("error") or "",
        "created_at": doc.get("created_at") or "",
        "updated_at": doc.get("updated_at") or "",
        "starter_seeded": bool(doc.get("starter_seeded")),
    }


def scrape_item_view(doc: dict[str, Any], preview_base_url: str = "") -> dict[str, Any]:
    slug = doc.get("slug") or "page"
    route = f"/research/{slug}.html"
    base = (preview_base_url or "").rstrip("/")
    return {
        "id": doc.get("id") or "",
        "workspace_id": doc.get("workspace_id") or "",
        "url": doc.get("url") or "",
        "slug": slug,
        "title": doc.get("title") or "",
        "excerpt": doc.get("excerpt") or "",
        "route": route,
        "page_url": f"{base}{route}" if base else "",
        "created_at": doc.get("created_at") or "",
    }
