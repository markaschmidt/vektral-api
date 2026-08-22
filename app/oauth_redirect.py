"""Shared OAuth redirect helpers for GitHub + Linear connect flows."""

from __future__ import annotations

from fastapi import HTTPException
from fastapi.responses import HTMLResponse

from app.config import get_settings


def html_redirect(url: str) -> HTMLResponse:
    attr = (
        url.replace("&", "&amp;")
        .replace('"', "&quot;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )
    js = url.replace("\\", "\\\\").replace('"', '\\"')
    body = (
        "<!doctype html><html><head>"
        f'<meta http-equiv="refresh" content="0;url={attr}">'
        f'<script>location.replace("{js}");</script>'
        "</head><body>Redirecting…</body></html>"
    )
    return HTMLResponse(content=body)


def integrations_home(query: str = "") -> str:
    base = f"{get_settings().web_origin}/integrations"
    return f"{base}?{query}" if query else base


def storage_unavailable() -> HTTPException:
    return HTTPException(
        status_code=503,
        detail={
            "error": "storage_unavailable",
            "message": "Firestore is unavailable. Enable Firestore in Firebase Console and retry.",
        },
    )
