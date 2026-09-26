"""Refresh GitHub/Linear OAuth access tokens before upstream calls.

GitHub App user tokens expire after 8 hours (refresh token 6 months):
https://docs.github.com/en/apps/creating-github-apps/authenticating-with-a-github-app/refreshing-user-access-tokens

Linear access tokens expire after 24 hours (refresh required as of 2026-04-01):
https://linear.app/developers/oauth-2-0-authentication

Do not return HTTP 502/504 for these failures. Cloudflare replaces origin 502/504
with a branded origin_bad_gateway page, which hides the real error and makes the
web client treat the list as empty.
https://developers.cloudflare.com/support/troubleshooting/http-status-codes/cloudflare-5xx-errors/error-502-504/
"""

from __future__ import annotations

import logging
import threading
from typing import Any

import httpx
from fastapi import HTTPException

from app.config import get_settings
from app.store import get_store
from app.timeutil import expires_at_from_seconds, is_expired

logger = logging.getLogger("vektral.oauth_refresh")

_lock_guard = threading.Lock()
_locks: dict[str, threading.Lock] = {}

# Failed Dependency — Cloudflare does not rewrite this the way it does 502/504.
UPSTREAM_STATUS = 424


def _lock(key: str) -> threading.Lock:
    with _lock_guard:
        lock = _locks.get(key)
        if lock is None:
            lock = threading.Lock()
            _locks[key] = lock
        return lock


def reauth_error(provider: str, message: str) -> HTTPException:
    return HTTPException(
        status_code=401,
        detail={"error": f"{provider}_reauth_required", "message": message},
    )


def upstream_error(provider: str, message: str) -> HTTPException:
    return HTTPException(
        status_code=UPSTREAM_STATUS,
        detail={"error": f"{provider}_upstream", "message": message},
    )


def ensure_github_access_token(uid: str, *, force: bool = False) -> str:
    with _lock(f"github:{uid}"):
        return _ensure_github(uid, force=force)


def ensure_linear_access_token(uid: str, *, force: bool = False) -> str:
    with _lock(f"linear:{uid}"):
        return _ensure_linear(uid, force=force)


def _ensure_github(uid: str, *, force: bool) -> str:
    creds = get_store().github_oauth_secrets(uid)
    access = creds.get("access_token") or ""
    refresh = creds.get("refresh_token") or ""
    expired = is_expired(creds.get("expires_at") or "")
    if access and not force and not expired:
        return access
    if refresh and (force or expired or not access):
        return _refresh_github(uid, refresh)
    if expired and not refresh:
        raise reauth_error(
            "github",
            "GitHub access expired. Reconnect GitHub under Integrations.",
        )
    return access


def _ensure_linear(uid: str, *, force: bool) -> str:
    creds = get_store().linear_oauth_secrets(uid)
    access = creds.get("access_token") or ""
    refresh = creds.get("refresh_token") or ""
    expired = is_expired(creds.get("expires_at") or "")
    if access and not force and not expired:
        return access
    if refresh and (force or expired or not access):
        return _refresh_linear(uid, refresh)
    if expired and not refresh:
        raise reauth_error(
            "linear",
            "Linear access expired. Reconnect Linear under Integrations.",
        )
    return access


def _refresh_github(uid: str, refresh_token: str) -> str:
    settings = get_settings()
    if not (settings.github_client_id and settings.github_client_secret):
        raise reauth_error("github", "GitHub OAuth is not configured.")
    try:
        with httpx.Client(timeout=20.0) as client:
            # GitHub documents refresh params as query string on this endpoint.
            resp = client.post(
                "https://github.com/login/oauth/access_token",
                headers={"Accept": "application/json"},
                params={
                    "client_id": settings.github_client_id,
                    "client_secret": settings.github_client_secret,
                    "grant_type": "refresh_token",
                    "refresh_token": refresh_token,
                },
            )
            payload: dict[str, Any] = resp.json() if resp.content else {}
    except httpx.HTTPError as exc:
        logger.exception("GitHub token refresh failed")
        raise upstream_error("github", str(exc)) from exc

    access = str(payload.get("access_token") or "")
    if resp.status_code >= 400 or not access:
        logger.warning("GitHub refresh rejected: %s", str(payload)[:200])
        raise reauth_error(
            "github",
            "GitHub access expired. Reconnect GitHub under Integrations.",
        )
    new_refresh = str(payload.get("refresh_token") or refresh_token)
    get_store().github_replace_tokens(
        uid,
        access_token=access,
        refresh_token=new_refresh,
        expires_at=expires_at_from_seconds(int(payload.get("expires_in") or 0)),
    )
    return access


def _refresh_linear(uid: str, refresh_token: str) -> str:
    settings = get_settings()
    if not (settings.linear_client_id and settings.linear_client_secret):
        raise reauth_error("linear", "Linear OAuth is not configured.")
    try:
        with httpx.Client(timeout=20.0) as client:
            resp = client.post(
                "https://api.linear.app/oauth/token",
                headers={
                    "Accept": "application/json",
                    "Content-Type": "application/x-www-form-urlencoded",
                },
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": refresh_token,
                    "client_id": settings.linear_client_id,
                    "client_secret": settings.linear_client_secret,
                },
            )
            payload: dict[str, Any] = resp.json() if resp.content else {}
    except httpx.HTTPError as exc:
        logger.exception("Linear token refresh failed")
        raise upstream_error("linear", str(exc)) from exc

    access = str(payload.get("access_token") or "")
    if resp.status_code >= 400 or not access:
        logger.warning("Linear refresh rejected: %s", str(payload)[:200])
        raise reauth_error(
            "linear",
            "Linear access expired. Reconnect Linear under Integrations.",
        )
    new_refresh = str(payload.get("refresh_token") or refresh_token)
    get_store().linear_replace_tokens(
        uid,
        access_token=access,
        refresh_token=new_refresh,
        expires_at=expires_at_from_seconds(int(payload.get("expires_in") or 0)),
    )
    return access


def github_connection_health(uid: str) -> dict[str, Any]:
    """Firestore connection flags plus whether the stored token still works."""
    st = get_store().github_status(uid)
    st["needs_reauth"] = False
    if not st.get("connected"):
        return st
    try:
        token = ensure_github_access_token(uid)
        st["needs_reauth"] = not bool(token)
    except HTTPException:
        st["needs_reauth"] = True
    return st
