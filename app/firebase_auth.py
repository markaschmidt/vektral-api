"""Firebase Admin ID-token verification.

Local / CI stub mode
--------------------
If ``GOOGLE_APPLICATION_CREDENTIALS`` points at a missing file, or Firebase
Admin cannot initialize, the API still starts. Auth-gated routes then return
**503** with ``firebase_unavailable`` so developers can smoke-test ``/healthz``
and unauthenticated public routes without a service account.

Set ``FIREBASE_STUB_MODE=false`` to fail fast at startup when credentials are
missing (recommended in production).
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any

from fastapi import HTTPException

from app.config import get_settings

logger = logging.getLogger("vektral.firebase")

_app: Any = None
_init_attempted = False
_init_error: str | None = None


@dataclass(frozen=True)
class VerifiedUser:
    uid: str
    email: str
    name: str
    picture: str
    claims: dict[str, Any]


def firebase_status() -> dict[str, Any]:
    """Report whether Admin SDK is ready (for /healthz)."""
    settings = get_settings()
    creds = settings.google_application_credentials
    return {
        "initialized": _app is not None,
        "stub_mode": settings.firebase_stub_mode,
        "project_id": settings.firebase_project_id or None,
        "credentials_path": creds or None,
        "credentials_present": bool(creds) and os.path.isfile(creds),
        "init_error": _init_error,
    }


def init_firebase() -> bool:
    """Initialize firebase-admin once. Returns True if verify is available."""
    global _app, _init_attempted, _init_error
    if _app is not None:
        return True
    if _init_attempted and _init_error:
        return False
    _init_attempted = True
    settings = get_settings()
    creds_path = settings.google_application_credentials
    if not creds_path or not os.path.isfile(creds_path):
        _init_error = (
            "GOOGLE_APPLICATION_CREDENTIALS unset or file missing "
            f"(looked for {creds_path or '(empty)'})"
        )
        if settings.firebase_stub_mode:
            logger.warning(
                "Firebase stub mode: %s — auth routes will return 503",
                _init_error,
            )
            return False
        raise RuntimeError(
            f"Firebase credentials required (FIREBASE_STUB_MODE=false): {_init_error}"
        )

    try:
        import firebase_admin
        from firebase_admin import credentials

        options: dict[str, Any] = {}
        if settings.firebase_project_id:
            options["projectId"] = settings.firebase_project_id
        cred = credentials.Certificate(creds_path)
        _app = firebase_admin.initialize_app(cred, options or None)
        _init_error = None
        logger.info("Firebase Admin initialized (project=%s)", settings.firebase_project_id)
        return True
    except Exception as exc:  # noqa: BLE001 — surface any init failure clearly
        _init_error = str(exc)
        if settings.firebase_stub_mode:
            logger.warning("Firebase stub mode: init failed: %s", exc)
            return False
        raise


def require_firebase() -> None:
    """Raise 503 when Admin SDK is not available."""
    if _app is not None:
        return
    if not _init_attempted:
        init_firebase()
    if _app is not None:
        return
    raise HTTPException(
        status_code=503,
        detail={
            "error": "firebase_unavailable",
            "message": (
                "Firebase Admin is not configured. Place a service account at "
                "the path in GOOGLE_APPLICATION_CREDENTIALS "
                "(see FIREBASE.md). Local stub mode allows the API to start "
                "without it."
            ),
            "init_error": _init_error,
        },
    )


def _dev_user_from_bearer(token: str) -> VerifiedUser | None:
    """Optional local bypass: ``Authorization: Bearer dev`` or ``dev:<uid>``."""
    settings = get_settings()
    if not settings.allow_dev_bearer:
        return None
    if token == "dev":
        uid = "dev-user"
    elif token.startswith("dev:"):
        uid = token[4:].strip() or "dev-user"
    else:
        return None
    return VerifiedUser(
        uid=uid,
        email=f"{uid}@dev.local",
        name=f"Dev {uid}",
        picture="",
        claims={"uid": uid, "dev": True},
    )


def verify_id_token(token: str) -> VerifiedUser:
    """Verify a Firebase ID token. Raises HTTPException on failure."""
    dev = _dev_user_from_bearer(token)
    if dev is not None:
        return dev

    require_firebase()
    try:
        from firebase_admin import auth as fb_auth

        claims = fb_auth.verify_id_token(token)
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status_code=401,
            detail={"error": "invalid_token", "message": f"invalid Firebase ID token: {exc}"},
        ) from exc

    uid = str(claims.get("uid") or claims.get("sub") or "").strip()
    if not uid:
        raise HTTPException(
            status_code=401,
            detail={"error": "invalid_token", "message": "token missing uid"},
        )
    return VerifiedUser(
        uid=uid,
        email=str(claims.get("email") or ""),
        name=str(claims.get("name") or ""),
        picture=str(claims.get("picture") or ""),
        claims=dict(claims),
    )


def bearer_token(authorization: str | None) -> str:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(
            status_code=401,
            detail={"error": "missing_bearer", "message": "Authorization: Bearer <Firebase ID token> required"},
        )
    token = authorization.split(" ", 1)[1].strip()
    if not token:
        raise HTTPException(
            status_code=401,
            detail={"error": "missing_bearer", "message": "empty bearer token"},
        )
    return token
