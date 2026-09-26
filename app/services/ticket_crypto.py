"""HMAC ticket crypto shared with Collab (identical v1 wire format)."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from typing import Any


def ticket_secret(*candidates: str) -> str:
    for raw in candidates:
        if (raw or "").strip():
            return raw.strip()
    return ""


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64url_decode(text: str) -> bytes:
    pad = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + pad)


def sign_payload(payload: dict[str, Any], secret: str) -> str:
    body = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    encoded = _b64url(body)
    sig = hmac.new(secret.encode("utf-8"), f"v1.{encoded}".encode("utf-8"), hashlib.sha256).digest()
    return f"v1.{encoded}.{_b64url(sig)}"


def verify_token(token: str, secret: str) -> dict[str, Any]:
    raw = (token or "").strip()
    parts = raw.split(".")
    if len(parts) != 3 or parts[0] != "v1":
        raise ValueError("malformed token")
    encoded, sig = parts[1], parts[2]
    expected = hmac.new(
        secret.encode("utf-8"), f"v1.{encoded}".encode("utf-8"), hashlib.sha256
    ).digest()
    got = _b64url_decode(sig)
    if not hmac.compare_digest(expected, got):
        raise ValueError("bad signature")
    data = json.loads(_b64url_decode(encoded).decode("utf-8"))
    if not isinstance(data, dict):
        raise ValueError("invalid payload")
    exp = int(data.get("exp") or 0)
    if exp and exp < int(time.time()):
        raise ValueError("expired")
    return data


def issue_ticket(
    *,
    secret: str,
    session_id: str,
    uid: str,
    workspace_id: str,
    runtime_id: str,
    route: str,
    nonce: str,
    ttl: int = 60,
    generation: int = 0,
    pane_id: str = "",
    reload_version: int = 0,
    channel: str = "",
) -> tuple[str, dict[str, Any]]:
    now = int(time.time())
    payload = {
        "typ": "ticket",
        "sid": session_id,
        "uid": uid,
        "wid": workspace_id,
        "rid": runtime_id,
        "route": route or "/",
        "nonce": nonce,
        "gen": int(generation),
        "pid": pane_id or "",
        "rv": int(reload_version or 0),
        "ch": channel or "",
        "iat": now,
        "exp": now + int(ttl),
    }
    return sign_payload(payload, secret), payload


def origin_for_session(template: str, session_id: str) -> str:
    origin = (template or "http://p-{session}.localhost:8790").replace("{session}", session_id)
    return origin.rstrip("/")
