"""Linear connect — OAuth for issue (card) access, cloned from GitHub."""

from __future__ import annotations

import logging
import secrets
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta, timezone
from typing import Annotated, Any
from urllib.parse import quote, urlencode

import httpx
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import HTMLResponse
from google.api_core import exceptions as gcp_exceptions

from app.config import get_settings
from app.deps import current_user
from app.firebase_auth import VerifiedUser
from app.oauth_redirect import html_redirect, integrations_home, storage_unavailable
from app.schemas import (
    LinearConnectStart,
    LinearDefaultsBody,
    LinearOrganizationView,
    LinearProjectCreateBody,
    LinearProjectView,
    LinearStatus,
    LinearTeamView,
)
from app.services import linear_client as linear_svc
from app.services.oauth_refresh import ensure_linear_access_token
from app.store import get_store

logger = logging.getLogger("vektral.linear")

router = APIRouter(tags=["linear"])


def _oauth_configured() -> bool:
    s = get_settings()
    return bool(s.linear_client_id and s.linear_client_secret)


def _redirect_uri() -> str:
    s = get_settings()
    if s.linear_oauth_redirect_uri:
        return s.linear_oauth_redirect_uri
    return f"{s.api_public_url}/sso/linear/callback"


def _token_for(uid: str, *, force: bool = False) -> str:
    return ensure_linear_access_token(uid, force=force)


async def _call_linear(
    uid: str,
    call: Callable[[str], Awaitable[Any]],
    *,
    empty: Any,
) -> Any:
    token = _token_for(uid)
    if not token:
        return empty
    try:
        return await call(token)
    except HTTPException as exc:
        if exc.status_code != 401:
            raise
        token = _token_for(uid, force=True)
        if not token:
            raise
        return await call(token)


@router.get("/api/linear/status", response_model=LinearStatus)
async def linear_status(
    user: Annotated[VerifiedUser, Depends(current_user)],
) -> dict[str, Any]:
    try:
        st = get_store().linear_status(user.uid)
    except gcp_exceptions.GoogleAPICallError as exc:
        logger.exception("Linear status Firestore read failed")
        raise storage_unavailable() from exc
    st["configured"] = _oauth_configured()
    return st


@router.post("/api/linear/connect", response_model=LinearConnectStart)
@router.get("/api/linear/connect", response_model=LinearConnectStart)
async def linear_connect(
    user: Annotated[VerifiedUser, Depends(current_user)],
) -> dict[str, Any]:
    settings = get_settings()
    if not _oauth_configured():
        return {
            "url": "",
            "state": "",
            "message": (
                "Linear OAuth not configured. Set LINEAR_CLIENT_ID and "
                "LINEAR_CLIENT_SECRET (see .env.example)."
            ),
        }
    state = secrets.token_urlsafe(24)
    try:
        get_store().linear_save_pending(state, user.uid)
    except gcp_exceptions.GoogleAPICallError as exc:
        logger.exception("Linear connect Firestore write failed")
        raise storage_unavailable() from exc
    params = urlencode(
        {
            "client_id": settings.linear_client_id,
            "redirect_uri": _redirect_uri(),
            "response_type": "code",
            "scope": settings.linear_oauth_scopes,
            "state": state,
            "prompt": "consent",
            "actor": "user",
        }
    )
    return {
        "url": f"https://linear.app/oauth/authorize?{params}",
        "state": state,
        "message": "",
    }


@router.delete("/api/linear/connect")
async def linear_disconnect(
    user: Annotated[VerifiedUser, Depends(current_user)],
) -> dict[str, Any]:
    get_store().linear_disconnect(user.uid)
    return {"ok": True, "message": "linear disconnected"}


@router.get("/api/linear/organizations", response_model=list[LinearOrganizationView])
async def list_linear_organizations(
    user: Annotated[VerifiedUser, Depends(current_user)],
) -> list[dict[str, Any]]:
    return await _call_linear(user.uid, linear_svc.list_organizations, empty=[])


@router.get("/api/linear/teams", response_model=list[LinearTeamView])
async def list_linear_teams(
    user: Annotated[VerifiedUser, Depends(current_user)],
    organization_id: str = "",
) -> list[dict[str, Any]]:
    return await _call_linear(
        user.uid,
        lambda token: linear_svc.list_teams(token, organization_id=organization_id),
        empty=[],
    )


@router.get("/api/linear/projects", response_model=list[LinearProjectView])
async def list_linear_projects(
    user: Annotated[VerifiedUser, Depends(current_user)],
    team_id: str = "",
) -> list[dict[str, Any]]:
    """Linear workspace projects for the connected account (optional team filter)."""
    return await _call_linear(
        user.uid,
        lambda token: linear_svc.list_projects(token, team_id=(team_id or "").strip()),
        empty=[],
    )


@router.post("/api/linear/projects", response_model=LinearProjectView, status_code=201)
async def create_linear_project(
    body: LinearProjectCreateBody,
    user: Annotated[VerifiedUser, Depends(current_user)],
) -> dict[str, Any]:
    """Create a Linear workspace project (paired with a Vektral project)."""
    token = _token_for(user.uid)
    if not token:
        raise HTTPException(
            status_code=400,
            detail={"error": "linear_not_connected", "message": "Connect Linear first"},
        )
    team_id = (body.team_id or "").strip()
    if not team_id:
        try:
            status = get_store().linear_status(user.uid)
        except gcp_exceptions.GoogleAPICallError as exc:
            logger.exception("Linear status Firestore read failed")
            raise storage_unavailable() from exc
        team_id = str(status.get("default_team_id") or "").strip()
    async def _create(token: str) -> dict[str, Any]:
        return await linear_svc.create_project(
            token,
            name=body.name,
            description=body.description or "",
            team_id=team_id,
        )

    try:
        return await _create(token)
    except HTTPException as exc:
        if exc.status_code != 401:
            raise
        token = _token_for(user.uid, force=True)
        if not token:
            raise
        return await _create(token)


@router.post("/api/linear/defaults", response_model=LinearStatus)
async def set_linear_defaults(
    body: LinearDefaultsBody,
    user: Annotated[VerifiedUser, Depends(current_user)],
) -> dict[str, Any]:
    try:
        result = get_store().linear_set_defaults(
            user.uid,
            organization_id=body.organization_id or "",
            team_id=body.team_id or "",
        )
    except gcp_exceptions.GoogleAPICallError as exc:
        logger.exception("Linear defaults Firestore write failed")
        raise storage_unavailable() from exc
    if result is None:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "linear_not_connected",
                "message": "Connect Linear before choosing a default team.",
            },
        )
    result["configured"] = _oauth_configured()
    return result


@router.get("/sso/linear/callback")
async def linear_oauth_callback(
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
    error_description: str | None = None,
) -> HTMLResponse:
    """Anonymous Linear OAuth callback — exchanges code, stores encrypted token."""
    settings = get_settings()

    if error:
        msg = quote(error_description or error or "oauth_error")
        return html_redirect(integrations_home(f"linear=error&message={msg}"))

    if not state:
        return html_redirect(integrations_home("linear=error&message=invalid_state"))

    store = get_store()
    try:
        pending = store.linear_get_pending(state)
    except gcp_exceptions.GoogleAPICallError:
        logger.exception("Linear callback Firestore read failed")
        return html_redirect(
            integrations_home("linear=error&message=storage_unavailable")
        )

    if not pending or pending.get("status") != "pending":
        return html_redirect(integrations_home("linear=error&message=invalid_state"))

    if not code:
        return html_redirect(integrations_home("linear=error&message=missing_code"))

    if not _oauth_configured():
        return html_redirect(integrations_home("linear=error&message=not_configured"))

    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            tok = await client.post(
                "https://api.linear.app/oauth/token",
                headers={"Accept": "application/json"},
                data={
                    "grant_type": "authorization_code",
                    "client_id": settings.linear_client_id,
                    "client_secret": settings.linear_client_secret,
                    "code": code,
                    "redirect_uri": _redirect_uri(),
                },
            )
            payload = tok.json() if tok.content else {}
            access = payload.get("access_token") or ""
            refresh = payload.get("refresh_token") or ""
            scopes = payload.get("scope") or settings.linear_oauth_scopes
            expires_in = int(payload.get("expires_in") or 0)
            if not access:
                return html_redirect(
                    integrations_home("linear=error&message=token_exchange_failed")
                )

            info = await linear_svc.viewer(access)
    except Exception:  # noqa: BLE001
        logger.exception("Linear OAuth exchange failed")
        return html_redirect(integrations_home("linear=error&message=exchange_failed"))

    expires_at = ""
    if expires_in > 0:
        expires_at = (
            datetime.now(timezone.utc) + timedelta(seconds=expires_in)
        ).isoformat()

    try:
        result = store.linear_complete(
            state,
            user_id=str(info.get("id") or ""),
            name=str(info.get("name") or ""),
            email=str(info.get("email") or ""),
            access_token=access,
            refresh_token=refresh,
            expires_at=expires_at,
            scopes=str(scopes or ""),
        )
    except gcp_exceptions.GoogleAPICallError:
        logger.exception("Linear callback Firestore write failed")
        return html_redirect(
            integrations_home("linear=error&message=storage_unavailable")
        )

    if result is None:
        return html_redirect(integrations_home("linear=error&message=complete_failed"))

    return html_redirect(integrations_home(f"linear=connected&state={quote(state)}"))
