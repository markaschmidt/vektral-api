"""KV store for VR agent state (panes, jobs, scrapes, preview_sessions).

Domain workspaces/orgs/github live in ``app.store.get_store()``. This module
holds agent-specific collections with Firestore + in-memory fallback.
"""

from __future__ import annotations

import logging
import threading
import uuid
from typing import Any

from app.timeutil import now_iso

logger = logging.getLogger("vektral.agent_kv")

_lock = threading.RLock()
_memory: dict[str, dict[str, dict[str, Any]]] = {
    "panes": {},
    "jobs": {},
    "scrapes": {},
    "preview_sessions": {},
    "messages": {},
    "pane_state": {},
}


def new_id(prefix: str = "") -> str:
    raw = uuid.uuid4().hex
    return f"{prefix}{raw}" if prefix else raw


def _db():
    try:
        from app.config import get_settings
        from app.firebase_auth import firebase_status

        settings = get_settings()
        if settings.use_memory_store:
            return None
        if not firebase_status().get("initialized"):
            return None
        from firebase_admin import firestore

        return firestore.client()
    except Exception as exc:  # noqa: BLE001
        logger.debug("agent_kv Firestore unavailable: %s", exc)
        return None


def put_doc(collection: str, doc_id: str, data: dict[str, Any]) -> dict[str, Any]:
    payload = {**data, "id": doc_id}
    db = _db()
    if db is not None:
        try:
            db.collection(collection).document(doc_id).set(payload, merge=True)
            return payload
        except Exception as exc:  # noqa: BLE001
            logger.warning("agent_kv put %s/%s failed: %s", collection, doc_id, exc)
    with _lock:
        col = _memory.setdefault(collection, {})
        prev = col.get(doc_id) or {}
        merged = {**prev, **payload}
        col[doc_id] = merged
        return dict(merged)


def get_doc(collection: str, doc_id: str) -> dict[str, Any] | None:
    db = _db()
    if db is not None:
        try:
            snap = db.collection(collection).document(doc_id).get()
            if snap.exists:
                data = snap.to_dict() or {}
                data.setdefault("id", doc_id)
                return data
        except Exception as exc:  # noqa: BLE001
            logger.warning("agent_kv get %s/%s failed: %s", collection, doc_id, exc)
    with _lock:
        doc = _memory.get(collection, {}).get(doc_id)
        return dict(doc) if doc else None


def query_eq(collection: str, field: str, value: Any) -> list[dict[str, Any]]:
    db = _db()
    if db is not None:
        try:
            snaps = db.collection(collection).where(field, "==", value).stream()
            out: list[dict[str, Any]] = []
            for snap in snaps:
                data = snap.to_dict() or {}
                data.setdefault("id", snap.id)
                out.append(data)
            return out
        except Exception as exc:  # noqa: BLE001
            logger.warning("agent_kv query %s failed: %s", collection, exc)
    with _lock:
        return [
            dict(doc)
            for doc in _memory.get(collection, {}).values()
            if doc.get(field) == value
        ]


def clear_memory() -> None:
    """Reset in-memory agent collections (tests)."""
    with _lock:
        for col in _memory.values():
            col.clear()


__all__ = ["now_iso", "new_id", "put_doc", "get_doc", "query_eq", "clear_memory"]
