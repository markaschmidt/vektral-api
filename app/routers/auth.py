"""Auth bridge — /auth/me, /auth/lobby-config."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from app.config import get_settings
from app.deps import current_profile, current_user
from app.firebase_auth import VerifiedUser
from app.profiles import update_profile

router = APIRouter(prefix="/auth", tags=["auth"])


class ProfilePatch(BaseModel):
    display_name: str | None = None
    avatar_url: str | None = None


class LobbyConfig(BaseModel):
    vr_web_origin: str = ""
    vr_auto_open_after_login: bool = False
    sso_vr_login_path: str = "/sso/vr"
    web_origin: str = Field(description="Vektral-Web origin used for VR bridge redirects")


@router.get("/lobby-config")
def lobby_config() -> LobbyConfig:
    """Public lobby settings (no auth)."""
    settings = get_settings()
    return LobbyConfig(
        vr_web_origin=settings.vr_web_origin,
        vr_auto_open_after_login=settings.vr_auto_open,
        sso_vr_login_path="/sso/vr",
        web_origin=settings.web_origin,
    )


@router.get("/me")
async def get_me(
    profile: Annotated[dict[str, Any], Depends(current_profile)],
) -> dict[str, Any]:
    """Verify Firebase Bearer; upsert and return profile."""
    return profile


@router.patch("/me")
async def patch_me(
    body: ProfilePatch,
    user: Annotated[VerifiedUser, Depends(current_user)],
) -> dict[str, Any]:
    return update_profile(user, body.model_dump(exclude_unset=True))
