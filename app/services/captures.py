"""Opaque, expiring, per-pane captures. The model never sees client URLs."""

from __future__ import annotations

import base64
import logging
import time
from typing import Any

from fastapi import HTTPException

from app.agent_kv import get_doc, new_id, now_iso, put_doc, query_eq
from app.config import get_settings

logger = logging.getLogger("vektral.captures")

ALLOWED_TYPES = {"image/png", "image/jpeg", "image/webp"}
MAX_BYTES = 1_500_000
MAX_TEXT = 2000
MAX_CAPTURES = 8
MAX_MODEL_IMAGES = 4


def _metric(event: str, **fields: Any) -> None:
    logger.info("preview_metric event=%s %s", event, fields)


def strip_visible_text(raw: str) -> str:
    text = (raw or "").replace("\x00", "")
    lowered = text.lower()
    for needle in ("<script", "api_key", "secret", "password", "authorization:"):
        if needle in lowered:
            text = text.replace(needle, "")
            text = text.replace(needle.upper(), "")
    return text[:MAX_TEXT]


def create_capture(
    *,
    uid: str,
    workspace_id: str,
    pane_id: str,
    context_revision: int,
    content_type: str,
    body: bytes,
    visible_text: str = "",
    viewport: dict[str, Any] | None = None,
) -> dict[str, Any]:
    s = get_settings()
    if not s.captures_enabled:
        raise HTTPException(
            status_code=403,
            detail={"error": "captures_disabled", "message": "image captures are not enabled"},
        )
    ctype = (content_type or "").split(";")[0].strip().lower()
    if ctype not in ALLOWED_TYPES:
        _metric("capture_rejected", reason="content_type", workspace_id=workspace_id)
        raise HTTPException(
            status_code=400,
            detail={"error": "invalid_content_type", "message": "png, jpeg, or webp required"},
        )
    if not body or len(body) > MAX_BYTES:
        _metric("capture_rejected", reason="size", workspace_id=workspace_id)
        raise HTTPException(
            status_code=400,
            detail={"error": "invalid_size", "message": f"capture must be 1..{MAX_BYTES} bytes"},
        )
    cid = new_id("cap_")
    now = int(time.time())
    doc = {
        "id": cid,
        "uid": uid,
        "workspace_id": workspace_id,
        "pane_id": pane_id,
        "context_revision": int(context_revision or 0),
        "content_type": ctype,
        "body_b64": base64.b64encode(body).decode("ascii"),
        "byte_length": len(body),
        "visible_text": strip_visible_text(visible_text),
        "viewport": viewport or {},
        "created_at": now_iso(),
        "expires_at": now + s.capture_ttl_seconds,
    }
    put_doc("captures", cid, doc)
    return capture_view(doc)


def capture_view(doc: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": doc.get("id") or "",
        "pane_id": doc.get("pane_id") or "",
        "context_revision": int(doc.get("context_revision") or 0),
        "captured_at": doc.get("created_at") or "",
        "image_ref": doc.get("id") or "",
        "visible_text": doc.get("visible_text") or "",
        "viewport": doc.get("viewport") or {},
        "content_type": doc.get("content_type") or "",
        "byte_length": int(doc.get("byte_length") or 0),
        "expires_at": int(doc.get("expires_at") or 0),
    }


def get_capture(capture_id: str, uid: str, workspace_id: str = "") -> dict[str, Any] | None:
    doc = get_doc("captures", capture_id)
    if not doc:
        return None
    if doc.get("uid") != uid:
        return None
    if workspace_id and doc.get("workspace_id") != workspace_id:
        return None
    if int(doc.get("expires_at") or 0) < int(time.time()):
        _metric("capture_rejected", reason="expired", capture_id=capture_id)
        return None
    if not isinstance(doc.get("bytes"), (bytes, bytearray)):
        try:
            doc["bytes"] = base64.b64decode(str(doc.get("body_b64") or ""))
        except Exception:  # noqa: BLE001
            doc["bytes"] = b""
    return doc


def pair_captures_to_targets(
    *,
    uid: str,
    workspace_id: str,
    targets: list[dict[str, Any]],
    captures: list[dict[str, Any]],
    context_revision: int,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Keep captures that match immutable targets+revision. Ignore the rest."""
    allowed = {str(t.get("pane_id") or "") for t in targets}
    order = [str(t.get("pane_id") or "") for t in targets]
    reasons: list[str] = []
    by_pane: dict[str, dict[str, Any]] = {}
    for raw in captures[:MAX_CAPTURES]:
        if not isinstance(raw, dict):
            reasons.append("ignored_non_object")
            continue
        pane_id = str(raw.get("pane_id") or "")
        ref = str(raw.get("image_ref") or raw.get("id") or "")
        rev = int(raw.get("context_revision") or 0)
        if pane_id not in allowed:
            reasons.append(f"{pane_id or 'unknown'}:not_a_target")
            continue
        if context_revision and rev and rev != context_revision:
            reasons.append(f"{pane_id}:stale_revision")
            continue
        doc = get_capture(ref, uid, workspace_id) if ref else None
        view = {
            "pane_id": pane_id,
            "context_revision": context_revision or rev,
            "captured_at": str(raw.get("captured_at") or (doc or {}).get("created_at") or ""),
            "image_ref": (doc or {}).get("id") or "",
            "visible_text": strip_visible_text(str(raw.get("visible_text") or (doc or {}).get("visible_text") or "")),
            "viewport": raw.get("viewport") if isinstance(raw.get("viewport"), dict) else {},
            "available": bool(doc),
        }
        if ref and not doc:
            reasons.append(f"{pane_id}:expired_or_foreign_ref")
        by_pane[pane_id] = view
    paired = [by_pane[pid] for pid in order if pid in by_pane]
    return paired, reasons


def model_image_payloads(captures: list[dict[str, Any]], uid: str, workspace_id: str) -> tuple[list[str], str]:
    """Return data URLs built server-side from opaque captures. Never fetch client URLs."""
    urls: list[str] = []
    status = "ok"
    for cap in captures:
        if len(urls) >= MAX_MODEL_IMAGES:
            break
        ref = str(cap.get("image_ref") or "")
        if not ref:
            continue
        doc = get_capture(ref, uid, workspace_id)
        if not doc:
            status = "vision_unavailable"
            continue
        raw = doc.get("bytes")
        if not isinstance(raw, (bytes, bytearray)):
            status = "vision_unavailable"
            continue
        import base64

        b64 = base64.b64encode(bytes(raw)).decode("ascii")
        ctype = str(doc.get("content_type") or "image/png")
        urls.append(f"data:{ctype};base64,{b64}")
    if captures and not urls:
        status = "vision_unavailable"
    return urls, status
