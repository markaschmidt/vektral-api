"""GitHub connect — OAuth for repo access (preview clone tokens)."""

from __future__ import annotations

import logging
import secrets
from typing import Annotated, Any
from urllib.parse import quote, urlencode

import httpx
from fastapi import APIRouter, Depends, Query
from fastapi.responses import HTMLResponse
from google.api_core import exceptions as gcp_exceptions

from app.config import get_settings
from app.deps import current_user
from app.firebase_auth import VerifiedUser
from app.oauth_redirect import html_redirect, integrations_home, storage_unavailable
from app.services.oauth_refresh import github_connection_health
from app.schemas import (
    GithubConnectStart,
    GithubRepoVerifyRequest,
    GithubRepoVerifyResult,
    GithubRepoView,
    GithubStatus,
)
from app.store import get_store
from app.timeutil import expires_at_from_seconds

logger = logging.getLogger("vektral.github")

router = APIRouter(tags=["github"])


def _oauth_configured() -> bool:
    s = get_settings()
    return bool(s.github_client_id and s.github_client_secret)


def _redirect_uri() -> str:
    s = get_settings()
    if s.github_oauth_redirect_uri:
        return s.github_oauth_redirect_uri
    return f"{s.api_public_url}/sso/github/callback/repo"


@router.get("/api/github/status", response_model=GithubStatus)
async def github_status(
    user: Annotated[VerifiedUser, Depends(current_user)],
) -> dict[str, Any]:
    try:
        st = github_connection_health(user.uid)
    except gcp_exceptions.GoogleAPICallError as exc:
        logger.exception("GitHub status Firestore read failed")
        raise storage_unavailable() from exc
    st["configured"] = _oauth_configured()
    return st


@router.post("/api/github/connect", response_model=GithubConnectStart)
@router.get("/api/github/connect", response_model=GithubConnectStart)
async def github_connect(
    user: Annotated[VerifiedUser, Depends(current_user)],
) -> dict[str, Any]:
    settings = get_settings()
    if not _oauth_configured():
        return {
            "url": "",
            "state": "",
            "message": (
                "GitHub OAuth not configured. Set GITHUB_CLIENT_ID and "
                "GITHUB_CLIENT_SECRET (see .env.example)."
            ),
        }
    state = secrets.token_urlsafe(24)
    try:
        get_store().github_save_pending(state, user.uid)
    except gcp_exceptions.GoogleAPICallError as exc:
        logger.exception("GitHub connect Firestore write failed")
        raise storage_unavailable() from exc
    params = urlencode(
        {
            "client_id": settings.github_client_id,
            "redirect_uri": _redirect_uri(),
            "scope": settings.github_oauth_scopes,
            "state": state,
            "allow_signup": "false",
        }
    )
    return {
        "url": f"https://github.com/login/oauth/authorize?{params}",
        "state": state,
        "message": "",
    }


@router.delete("/api/github/connect")
async def github_disconnect(
    user: Annotated[VerifiedUser, Depends(current_user)],
) -> dict[str, Any]:
    get_store().github_disconnect(user.uid)
    return {"ok": True, "message": "github disconnected"}


@router.get("/api/github/repos", response_model=list[GithubRepoView])
async def list_github_repos(
    user: Annotated[VerifiedUser, Depends(current_user)],
    page: int = Query(1, ge=1, le=50),
    sort: str = Query(
        "pushed",
        description="GitHub sort: pushed, updated, created, or name (full_name)",
    ),
    per_page: int = Query(100, ge=1, le=100),
) -> list[dict[str, Any]]:
    """Wave 3 picker — works when connected; empty list otherwise."""
    from app.services.github_repos import list_user_repos

    return list_user_repos(user.uid, page=page, sort=sort, per_page=per_page)


@router.post("/api/github/repos/verify", response_model=list[GithubRepoVerifyResult])
async def verify_github_repos(
    body: GithubRepoVerifyRequest,
    user: Annotated[VerifiedUser, Depends(current_user)],
) -> list[dict[str, Any]]:
    """Check whether linked repos still exist and are accessible to the user."""
    from app.services.github_repos import verify_repo_access
    from app.services.oauth_refresh import ensure_github_access_token

    token = ensure_github_access_token(user.uid)
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for raw in body.repos:
        full_name = (raw or "").strip()
        if not full_name or full_name in seen:
            continue
        seen.add(full_name)
        out.append(verify_repo_access(full_name, token))
    return out


@router.get("/sso/github/callback/repo")
async def github_oauth_callback(
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
    error_description: str | None = None,
) -> HTMLResponse:
    """Anonymous GitHub OAuth callback — exchanges code, stores encrypted token."""
    settings = get_settings()

    if error:
        msg = quote(error_description or error or "oauth_error")
        return html_redirect(integrations_home(f"github=error&message={msg}"))

    if not state:
        return html_redirect(integrations_home("github=error&message=invalid_state"))

    store = get_store()
    try:
        pending = store.github_get_pending(state)
    except gcp_exceptions.GoogleAPICallError:
        logger.exception("GitHub callback Firestore read failed")
        return html_redirect(
            integrations_home("github=error&message=storage_unavailable")
        )

    if not pending or pending.get("status") != "pending":
        return html_redirect(integrations_home("github=error&message=invalid_state"))

    if not code:
        return html_redirect(integrations_home("github=error&message=missing_code"))

    if not _oauth_configured():
        return html_redirect(integrations_home("github=error&message=not_configured"))

    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            tok = await client.post(
                "https://github.com/login/oauth/access_token",
                headers={"Accept": "application/json"},
                data={
                    "client_id": settings.github_client_id,
                    "client_secret": settings.github_client_secret,
                    "code": code,
                    "redirect_uri": _redirect_uri(),
                },
            )
            payload = tok.json()
            access = payload.get("access_token") or ""
            refresh = payload.get("refresh_token") or ""
            scopes = payload.get("scope") or ""
            expires_in = int(payload.get("expires_in") or 0)
            if not access:
                return html_redirect(
                    integrations_home("github=error&message=token_exchange_failed")
                )

            from app.services.github_repos import github_headers

            user_resp = await client.get(
                "https://api.github.com/user",
                headers=github_headers(access),
            )
            user_json = user_resp.json() if user_resp.status_code < 400 else {}
            login = (user_json.get("login") or "").strip()
    except Exception:  # noqa: BLE001
        logger.exception("GitHub OAuth exchange failed")
        return html_redirect(integrations_home("github=error&message=exchange_failed"))

    try:
        result = store.github_complete(
            state,
            login=login,
            access_token=access,
            scopes=scopes,
            refresh_token=refresh,
            expires_at=expires_at_from_seconds(expires_in),
        )
    except gcp_exceptions.GoogleAPICallError:
        logger.exception("GitHub callback Firestore write failed")
        return html_redirect(
            integrations_home("github=error&message=storage_unavailable")
        )

    if result is None:
        return html_redirect(integrations_home("github=error&message=complete_failed"))

    return html_redirect(
        integrations_home(f"github=connected&state={quote(state)}")
    )
