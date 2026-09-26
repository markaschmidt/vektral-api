"""Authenticated preview launch tickets (API side).

Issues HMAC tickets that Collab redeems on an isolated origin. Public pane
views never include loopback URLs or internal ports.
"""

from __future__ import annotations

import logging
import secrets
import time
from typing import Any

from fastapi import HTTPException

from app.agent_kv import now_iso, put_doc
from app.config import get_settings
from app.timeutil import unix_to_iso

logger = logging.getLogger("vektral.preview_tickets")

# Keep the wire format identical to vektral-collab/preview/tickets.py
from app.services.ticket_crypto import (  # noqa: E402
    issue_ticket,
    origin_for_session,
    ticket_secret,
)


def _metric(event: str, **fields: Any) -> None:
    safe = {k: v for k, v in fields.items() if k not in {"ticket", "cookie", "token"}}
    logger.info("preview_metric event=%s %s", event, safe)


def issue_launch_ticket(
    *,
    uid: str,
    workspace_id: str,
    pane_id: str,
    runtime_id: str,
    route: str,
    session_id: str,
    generation: int,
    reload_version: int = 0,
    channel: str = "",
) -> dict[str, Any]:
    s = get_settings()
    secret = ticket_secret(s.preview_ticket_secret or s.preview_runner_token)
    if not secret:
        _metric("launch_ticket_denied", reason="no_secret", workspace_id=workspace_id)
        raise HTTPException(
            status_code=503,
            detail={"error": "preview_tickets_unconfigured", "message": "PREVIEW_TICKET_SECRET is required"},
        )
    nonce = secrets.token_hex(16)
    bridge_channel = (channel or "").strip() or secrets.token_hex(12)
    ticket, payload = issue_ticket(
        secret=secret,
        session_id=session_id,
        uid=uid,
        workspace_id=workspace_id,
        runtime_id=runtime_id,
        route=route or "/",
        nonce=nonce,
        ttl=s.preview_ticket_ttl_seconds,
        generation=generation,
        pane_id=pane_id,
        reload_version=reload_version,
        channel=bridge_channel,
    )
    origin = origin_for_session(s.preview_isolated_origin_template, session_id)
    launch_url = f"{origin}/__vektral_auth?ticket={ticket}"
    doc = {
        "id": nonce,
        "uid": uid,
        "workspace_id": workspace_id,
        "pane_id": pane_id,
        "runtime_id": runtime_id,
        "session_id": session_id,
        "route": route or "/",
        "generation": generation,
        "reload_version": int(reload_version or 0),
        "bridge_channel": bridge_channel,
        "expires_at": payload["exp"],
        "created_at": now_iso(),
    }
    put_doc("preview_tickets", nonce, doc)
    _metric("launch_ticket_issued", workspace_id=workspace_id, pane_id=pane_id)
    expires_iso = unix_to_iso(payload["exp"])
    return {
        "launch_url": launch_url,
        "session_id": session_id,
        "preview_origin": origin,
        "origin": origin,
        "launch_expires_at": expires_iso,
        "expires_at": payload["exp"],
        "reload_version": int(reload_version or 0),
        "bridge_channel": bridge_channel,
        "route": route or "/",
    }


def revoke_workspace_tickets(workspace_id: str) -> None:
    """Best-effort audit stamp; Collab generation bump is the live revocation."""
    put_doc(
        "preview_tickets",
        f"revoke_{workspace_id}_{int(time.time())}",
        {"workspace_id": workspace_id, "revoked_at": now_iso()},
    )
