"""GitHub repository helpers for workspace creation."""

from __future__ import annotations

import logging
import re
from typing import Any

import httpx
from fastapi import HTTPException

from app.store import get_store

logger = logging.getLogger("vektral.github_repos")

_REPO_NAME_RE = re.compile(r"[^a-z0-9._-]+")


def slugify_repo_name(name: str) -> str:
    base = (name or "untitled").strip().lower()
    base = _REPO_NAME_RE.sub("-", base).strip("-")
    if not base:
        base = "untitled"
    return base[:100]


def create_user_repo(
    uid: str,
    *,
    name: str,
    description: str = "",
    private: bool = True,
) -> dict[str, Any]:
    token = get_store().github_access_token(uid)
    if not token:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "github_not_connected",
                "message": "Connect GitHub under Integrations before creating a repository.",
            },
        )

    repo_name = slugify_repo_name(name)
    payload = {
        "name": repo_name,
        "description": (description or "").strip()[:350],
        "private": private,
        "auto_init": True,
    }

    try:
        with httpx.Client(timeout=30.0) as client:
            resp = client.post(
                "https://api.github.com/user/repos",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/vnd.github+json",
                    "User-Agent": "Vektral-API",
                },
                json=payload,
            )
    except httpx.HTTPError as exc:
        logger.exception("GitHub create repo request failed")
        raise HTTPException(
            status_code=502,
            detail={"error": "github_upstream", "message": str(exc)},
        ) from exc

    if resp.status_code == 422:
        detail = resp.json() if resp.content else {}
        message = detail.get("message") if isinstance(detail, dict) else "repo name unavailable"
        errors = detail.get("errors") if isinstance(detail, dict) else None
        if isinstance(errors, list) and errors:
            first = errors[0]
            if isinstance(first, dict) and first.get("message"):
                message = str(first["message"])
        raise HTTPException(
            status_code=409,
            detail={"error": "github_repo_exists", "message": message or "repository already exists"},
        )

    if resp.status_code >= 400:
        logger.warning("GitHub create repo failed: %s", resp.text[:300])
        raise HTTPException(
            status_code=502,
            detail={
                "error": "github_upstream",
                "message": "failed to create GitHub repository",
            },
        )

    data = resp.json()
    return {
        "full_name": data.get("full_name") or "",
        "default_branch": data.get("default_branch") or "main",
        "html_url": data.get("html_url") or "",
        "private": bool(data.get("private")),
    }


def rename_user_repo(
    uid: str,
    full_name: str,
    project_name: str,
) -> dict[str, Any]:
    """Rename a GitHub repo to match a workspace title (repo segment only)."""
    token = get_store().github_access_token(uid)
    if not token:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "github_not_connected",
                "message": "Connect GitHub under Integrations before renaming a repository.",
            },
        )

    normalized = (full_name or "").strip()
    if not normalized or "/" not in normalized:
        return {"full_name": normalized, "renamed": False}

    owner, repo = normalized.split("/", 1)
    new_repo = slugify_repo_name(project_name)
    if not new_repo or new_repo == repo:
        return {"full_name": normalized, "renamed": False}

    url = f"https://api.github.com/repos/{owner}/{repo}"
    try:
        with httpx.Client(timeout=30.0) as client:
            resp = client.patch(
                url,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/vnd.github+json",
                    "User-Agent": "Vektral-API",
                },
                json={"name": new_repo},
            )
    except httpx.HTTPError as exc:
        logger.exception("GitHub rename repo request failed")
        raise HTTPException(
            status_code=502,
            detail={"error": "github_upstream", "message": str(exc)},
        ) from exc

    if resp.status_code == 404:
        raise HTTPException(
            status_code=409,
            detail={
                "error": "repo_inaccessible",
                "message": "GitHub repository was deleted or could not be found.",
            },
        )
    if resp.status_code in {401, 403}:
        raise HTTPException(
            status_code=409,
            detail={
                "error": "repo_inaccessible",
                "message": "GitHub repository is no longer accessible with your account.",
            },
        )
    if resp.status_code == 422:
        detail = resp.json() if resp.content else {}
        message = detail.get("message") if isinstance(detail, dict) else "repo name unavailable"
        errors = detail.get("errors") if isinstance(detail, dict) else None
        if isinstance(errors, list) and errors:
            first = errors[0]
            if isinstance(first, dict) and first.get("message"):
                message = str(first["message"])
        raise HTTPException(
            status_code=409,
            detail={"error": "github_repo_exists", "message": message or "repository name unavailable"},
        )
    if resp.status_code >= 400:
        logger.warning("GitHub rename repo failed: %s", resp.text[:300])
        raise HTTPException(
            status_code=502,
            detail={
                "error": "github_upstream",
                "message": "failed to rename GitHub repository",
            },
        )

    data = resp.json()
    return {
        "full_name": data.get("full_name") or f"{owner}/{new_repo}",
        "renamed": True,
    }


def verify_repo_access(full_name: str, token: str | None) -> dict[str, Any]:
    """Return accessibility for a repo full name using the user's GitHub token."""
    normalized = (full_name or "").strip()
    if not normalized or "/" not in normalized:
        return {
            "full_name": normalized,
            "accessible": False,
            "reason": "invalid_repo_name",
        }
    if not token:
        return {
            "full_name": normalized,
            "accessible": False,
            "reason": "github_not_connected",
        }

    url = f"https://api.github.com/repos/{normalized}"
    try:
        with httpx.Client(timeout=15.0) as client:
            resp = client.get(
                url,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/vnd.github+json",
                    "User-Agent": "Vektral-API",
                },
            )
    except httpx.HTTPError as exc:
        logger.warning("GitHub verify repo failed for %s: %s", normalized, exc)
        return {
            "full_name": normalized,
            "accessible": False,
            "reason": "github_upstream",
        }

    if resp.status_code == 200:
        return {"full_name": normalized, "accessible": True, "reason": ""}
    if resp.status_code == 404:
        return {
            "full_name": normalized,
            "accessible": False,
            "reason": "not_found",
        }
    if resp.status_code in {401, 403}:
        return {
            "full_name": normalized,
            "accessible": False,
            "reason": "forbidden",
        }
    return {
        "full_name": normalized,
        "accessible": False,
        "reason": "github_upstream",
    }
