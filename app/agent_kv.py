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
    "preview_tickets": {},
    "captures": {},
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


def increment_counter(collection: str, doc_id: str, field: str = "revision") -> int:
    """Process-local atomic increment used for plan/turn revisions."""
    with _lock:
        doc = get_doc(collection, doc_id) or {}
        n = int(doc.get(field) or 0) + 1
        payload = {**doc, "id": doc_id, field: n, "updated_at": now_iso()}
        put_doc(collection, doc_id, payload)
        return n


def create_if_absent(
    collection: str, doc_id: str, data: dict[str, Any]
) -> tuple[bool, dict[str, Any]]:
    """Atomically create ``data`` when ``doc_id`` is missing.

    Firestore uses a transaction; the in-memory store uses the module lock.
    Returns ``(created, current_document)``.
    """
    payload = {**data, "id": doc_id}
    db = _db()
    if db is not None:
        try:
            from firebase_admin import firestore as fb_fs

            ref = db.collection(collection).document(doc_id)

            @fb_fs.transactional
            def _txn(transaction):  # type: ignore[no-untyped-def]
                snap = ref.get(transaction=transaction)
                if snap.exists:
                    existing = snap.to_dict() or {}
                    existing.setdefault("id", doc_id)
                    return False, existing
                transaction.set(ref, payload)
                return True, dict(payload)

            return _txn(db.transaction())
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "agent_kv create_if_absent %s/%s failed: %s", collection, doc_id, exc
            )
    with _lock:
        col = _memory.setdefault(collection, {})
        existing = col.get(doc_id)
        if existing is not None:
            return False, dict(existing)
        stored = dict(payload)
        col[doc_id] = stored
        return True, dict(stored)


def clear_memory() -> None:
    """Reset in-memory agent collections (tests)."""
    with _lock:
        for col in _memory.values():
            col.clear()


__all__ = [
    "now_iso",
    "new_id",
    "put_doc",
    "get_doc",
    "query_eq",
    "increment_counter",
    "create_if_absent",
    "clear_memory",
]
