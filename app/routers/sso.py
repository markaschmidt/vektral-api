"""VR SSO bridge — allowlisted redirect into Vektral-Web /auth/vr."""

from __future__ import annotations

from urllib.parse import urlencode

from fastapi import APIRouter, Query
from fastapi.responses import HTMLResponse

from app.config import get_settings
from app.sso_allowlist import is_vr_callback_allowed

router = APIRouter(tags=["sso"])

_PLATFORMS = frozenset({"google", "github"})


def _html_error(message: str, status: int = 400) -> HTMLResponse:
    safe = (
        message.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )
    return HTMLResponse(
        content=(
            "<!doctype html><html><body>"
            f"<p>SSO blocked: {safe}</p>"
            "</body></html>"
        ),
        status_code=status,
    )


def _html_redirect(url: str) -> HTMLResponse:
    attr = (
        url.replace("&", "&amp;")
        .replace('"', "&quot;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )
    js = url.replace("\\", "\\\\").replace('"', '\\"')
    return HTMLResponse(
        content=(
            "<!doctype html><html><head>"
            f'<meta http-equiv="refresh" content="0;url={attr}">'
            f'<script>location.replace("{js}");</script>'
            "</head><body>Redirecting…</body></html>"
        ),
        status_code=200,
    )


@router.get("/sso/vr/{platform}/login")
def vr_sso_login(
    platform: str,
    client_callback: str = Query(default=""),
) -> HTMLResponse:
    """Bridge VR login through Vektral-Web Firebase sign-in.

    Flow (Wave 1 stub → Wave 2 Web completes sign-in):
    1. Validate ``client_callback`` against ``SSO_VR_CALLBACK_ALLOWLIST``
       (loopback always allowed).
    2. Redirect browser to ``{WEB_ORIGIN}/auth/vr?platform=&client_callback=``.
    3. Web signs in with Firebase and returns to
       ``{client_callback}?token=<Firebase ID token>``.
    """
    cb = client_callback.strip()
    if not cb:
        return _html_error("client_callback is required")
    if not cb.endswith("/"):
        cb = f"{cb}/"

    plat = platform.strip().lower()
    if plat not in _PLATFORMS:
        return _html_error("Unsupported SSO platform (use google or github)")

    settings = get_settings()
    if not is_vr_callback_allowed(cb, settings.sso_vr_allowlist):
        return _html_error("VR callback origin is not allowlisted", status=403)

    qs = urlencode({"platform": plat, "client_callback": cb})
    target = f"{settings.web_app_origin()}/auth/vr?{qs}"
    return _html_redirect(target)
