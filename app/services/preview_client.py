"""HTTP client for Vektral-Collab preview runner."""

from __future__ import annotations

import logging
from typing import Any

import httpx

from app.config import get_settings

logger = logging.getLogger("vektral.preview_client")


def _base() -> str:
    s = get_settings()
    return s.preview_runner_url


def _headers() -> dict[str, str]:
    s = get_settings()
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    if s.preview_runner_token:
        headers["Authorization"] = f"Bearer {s.preview_runner_token}"
    return headers


async def preview_runner_request(
    method: str,
    path: str,
    body: dict[str, Any] | None = None,
    timeout: float = 300.0,
) -> dict[str, Any]:
    url = f"{_base()}{path}"
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            kwargs: dict[str, Any] = {"headers": _headers()}
            if body is not None:
                kwargs["json"] = body
            resp = await client.request(method, url, **kwargs)
            try:
                data = resp.json()
            except Exception:  # noqa: BLE001
                data = {"ok": False, "error": f"invalid JSON from runner ({resp.status_code})", "raw": resp.text[:800]}
            if not isinstance(data, dict):
                return {"ok": False, "error": "invalid runner response", "raw": str(data)[:800]}
            if resp.status_code >= 400 and "ok" not in data:
                data = {"ok": False, "error": f"runner HTTP {resp.status_code}: {data}"}
            return data
    except httpx.HTTPError as exc:
        logger.warning("preview runner request failed: %s %s — %s", method, path, exc)
        return {"ok": False, "error": str(exc)}


async def git_tree(workspace_id: str, path: str = "") -> dict[str, Any]:
    from urllib.parse import quote

    suffix = f"?path={quote(path)}" if path else ""
    return await preview_runner_request(
        "GET",
        f"/v1/git/{workspace_id}/tree{suffix}",
        None,
        timeout=30.0,
    )


async def git_file(workspace_id: str, path: str) -> dict[str, Any]:
    from urllib.parse import quote

    return await preview_runner_request(
        "GET",
        f"/v1/git/{workspace_id}/file?path={quote(path)}",
        None,
        timeout=30.0,
    )


async def git_tree(workspace_id: str, path: str = "") -> dict[str, Any]:
    suffix = f"?path={path}" if path else ""
    return await preview_runner_request(
        "GET",
        f"/v1/git/{workspace_id}/tree{suffix}",
        None,
        timeout=30.0,
    )


async def git_file(workspace_id: str, path: str) -> dict[str, Any]:
    from urllib.parse import quote

    return await preview_runner_request(
        "GET",
        f"/v1/git/{workspace_id}/file?path={quote(path)}",
        None,
        timeout=30.0,
    )
