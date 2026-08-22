"""Vocal Bridge token mint — lifted from vektral/services/voice/app.py.

Returns upstream Vocal Bridge JSON **verbatim** (no Jac envelope) so an SDK
can use these paths as ``tokenUrl`` unchanged.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any

from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import JSONResponse

from app.config import get_settings
from app.firebase_auth import bearer_token, verify_id_token

router = APIRouter(tags=["voice"])

_PASSTHROUGH = ("participant_name", "participant_identity", "room_name", "metadata")


def _check_auth(authorization: str | None) -> None:
    settings = get_settings()
    if not settings.voice_require_auth:
        return
    token = bearer_token(authorization)
    # Prefer Firebase ID token (v2). verify_id_token raises 401/503 as needed.
    verify_id_token(token)


async def _payload(request: Request) -> dict[str, Any]:
    body: Any = {}
    if request.method != "GET":
        raw = await request.body()
        if raw:
            try:
                body = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise HTTPException(status_code=400, detail="body must be JSON") from exc
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="body must be a JSON object")
    merged = dict(request.query_params)
    merged.update(body)
    return merged


def _mint(payload: dict[str, Any]) -> JSONResponse:
    settings = get_settings()
    key = settings.vocalbridge_api_key
    if not key:
        raise HTTPException(status_code=503, detail="VOCALBRIDGE_API_KEY not configured")

    upstream: dict[str, Any] = {
        k: payload[k] for k in _PASSTHROUGH if payload.get(k) not in (None, "")
    }
    upstream.setdefault("participant_name", settings.voice_default_participant)

    headers = {
        "X-API-Key": key,
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": settings.voice_user_agent,
    }
    agent_id = str(payload.get("agent_id") or "").strip() or settings.vocalbridge_agent_id
    if agent_id:
        headers["X-Agent-Id"] = agent_id

    req = urllib.request.Request(
        f"{settings.vocalbridge_base_url}/api/v1/token",
        data=json.dumps(upstream).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=settings.voice_timeout) as resp:
            raw = resp.read().decode("utf-8")
            status = resp.status
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:500]
        try:
            parsed = json.loads(detail)
        except json.JSONDecodeError:
            parsed = {"detail": detail or f"Vocal Bridge HTTP {exc.code}"}
        return JSONResponse(status_code=exc.code, content=parsed)
    except Exception as exc:  # noqa: BLE001
        return JSONResponse(
            status_code=502, content={"detail": f"Vocal Bridge unreachable: {exc}"}
        )

    try:
        data = json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        return JSONResponse(
            status_code=502, content={"detail": "invalid Vocal Bridge response"}
        )
    if not isinstance(data, dict):
        return JSONResponse(
            status_code=502, content={"detail": "invalid Vocal Bridge response"}
        )

    live = data.get("livekit_url") or data.get("url") or ""
    if live:
        data.setdefault("url", live)
        data["livekit_url"] = live
    return JSONResponse(status_code=status, content=data)


@router.api_route("/api/v1/token", methods=["GET", "POST"])
@router.api_route("/api/voice-token", methods=["GET", "POST"])
@router.api_route("/api/voice/token", methods=["GET", "POST"])
async def token(
    request: Request,
    authorization: str | None = Header(default=None),
) -> JSONResponse:
    _check_auth(authorization)
    return _mint(await _payload(request))
