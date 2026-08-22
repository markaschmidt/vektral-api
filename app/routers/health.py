"""Liveness / readiness."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from app.config import get_settings
from app.firebase_auth import firebase_status

router = APIRouter(tags=["health"])


@router.get("/health")
@router.get("/healthz")
def healthz() -> dict[str, Any]:
    settings = get_settings()
    fb = firebase_status()
    return {
        "ok": True,
        "service": "vektral-api",
        "firebase": fb,
        "voice": {
            "configured": bool(settings.vocalbridge_api_key),
            "agent_id_set": bool(settings.vocalbridge_agent_id),
            "require_auth": settings.voice_require_auth,
            "upstream": settings.vocalbridge_base_url,
        },
    }
