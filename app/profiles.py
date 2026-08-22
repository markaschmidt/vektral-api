"""Firestore ``users/{uid}`` profile helpers.

When Firebase/Firestore is unavailable, callers already get 503 from
``require_firebase``. Upsert is best-effort: if Firestore fails we still
return a profile derived from the ID token claims.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from app.firebase_auth import VerifiedUser

logger = logging.getLogger("vektral.profiles")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def profile_from_user(user: VerifiedUser, stored: dict[str, Any] | None = None) -> dict[str, Any]:
    avatar = user.picture or (stored or {}).get("avatar_url") or (stored or {}).get("photo_url") or ""
    base = {
        "id": user.uid,
        "uid": user.uid,
        "display_name": user.name or (stored or {}).get("display_name") or "",
        "email": user.email or (stored or {}).get("email") or "",
        "avatar_url": avatar,
        "photo_url": avatar,
        "github_login": (stored or {}).get("github_login") or "",
        "github_connected": bool((stored or {}).get("github_connected")),
        "org_count": int((stored or {}).get("org_count") or 0),
        "is_independent": True,
        "root_id": user.uid,
    }
    if stored:
        for key in ("display_name", "avatar_url", "github_login"):
            if stored.get(key):
                base[key] = stored[key]
        if stored.get("photo_url") and not base["avatar_url"]:
            base["avatar_url"] = stored["photo_url"]
        base["photo_url"] = base["avatar_url"]
        if "github_connected" in stored:
            base["github_connected"] = bool(stored["github_connected"])
        if "org_count" in stored:
            base["org_count"] = int(stored["org_count"] or 0)
    return base


def get_or_upsert_profile(user: VerifiedUser) -> dict[str, Any]:
    from app.config import get_settings

    settings = get_settings()
    if settings.use_memory_store or (
        settings.allow_dev_bearer and user.claims.get("dev")
    ):
        # Keep profiles ephemeral in memory/dev unless Firestore is up
        try:
            from app.firebase_auth import firebase_status

            if not firebase_status().get("initialized"):
                return profile_from_user(user)
        except Exception:  # noqa: BLE001
            return profile_from_user(user)

    try:
        from firebase_admin import firestore

        db = firestore.client()
        ref = db.collection("users").document(user.uid)
        snap = ref.get()
        existing = snap.to_dict() if snap.exists else None
        profile = profile_from_user(user, existing)
        # Merge live github connection subdoc
        try:
            gsnap = ref.collection("github").document("connection").get()
            if gsnap.exists:
                g = gsnap.to_dict() or {}
                profile["github_connected"] = bool(g.get("connected"))
                profile["github_login"] = g.get("login") or profile.get("github_login") or ""
        except Exception:  # noqa: BLE001
            pass
        payload = {
            **profile,
            "updated_at": _now_iso(),
        }
        if not existing:
            payload["created_at"] = _now_iso()
        ref.set(payload, merge=True)
        return profile
    except Exception as exc:  # noqa: BLE001
        logger.warning("Firestore upsert failed for %s: %s — returning token profile", user.uid, exc)
        return profile_from_user(user)


def update_profile(user: VerifiedUser, patch: dict[str, Any]) -> dict[str, Any]:
    allowed = {"display_name", "avatar_url"}
    clean = {k: v for k, v in patch.items() if k in allowed and v is not None}
    try:
        from firebase_admin import firestore

        db = firestore.client()
        ref = db.collection("users").document(user.uid)
        snap = ref.get()
        existing = snap.to_dict() if snap.exists else {}
        merged = {**(existing or {}), **clean, "uid": user.uid, "updated_at": _now_iso()}
        if not existing:
            merged["created_at"] = _now_iso()
            merged.setdefault("email", user.email)
        ref.set(merged, merge=True)
        return profile_from_user(user, merged)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Firestore update failed for %s: %s", user.uid, exc)
        return profile_from_user(user, clean)
