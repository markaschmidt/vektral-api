"""FastAPI dependencies."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import Depends, Header, HTTPException

from app.firebase_auth import VerifiedUser, bearer_token, require_firebase, verify_id_token
from app.profiles import get_or_upsert_profile, update_profile


async def current_user(
    authorization: Annotated[str | None, Header()] = None,
) -> VerifiedUser:
    token = bearer_token(authorization)
    return verify_id_token(token)


async def current_profile(
    user: Annotated[VerifiedUser, Depends(current_user)],
) -> dict[str, Any]:
    """Verify token and ensure Firestore ``users/{uid}`` exists."""
    return get_or_upsert_profile(user)


def require_auth_ready() -> None:
    require_firebase()


__all__ = [
    "current_user",
    "current_profile",
    "require_auth_ready",
    "HTTPException",
    "update_profile",
]
