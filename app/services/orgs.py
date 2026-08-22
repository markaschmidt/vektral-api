"""Organization sharing — members, invites, join codes, ACL helpers."""

from __future__ import annotations

import secrets
from typing import Any

from fastapi import HTTPException

from app.store import get_store, org_to_view


VALID_ROLES = frozenset({"owner", "admin", "member"})
ASSIGNABLE_ROLES = frozenset({"admin", "member"})


def _caps(role: str) -> list[str]:
    if role == "owner":
        return ["manage", "invite", "create_workspace", "transfer"]
    if role == "admin":
        return ["manage", "invite", "create_workspace"]
    if role == "member":
        return ["create_workspace"]
    return []


def role_of(uid: str, org_id: str) -> str | None:
    return get_store().member_role(uid, org_id)


def require_membership(uid: str, org_id: str) -> str:
    role = role_of(uid, org_id)
    if not role:
        raise HTTPException(
            status_code=404,
            detail={"error": "not_found", "message": "organization not found"},
        )
    return role


def require_capability(uid: str, org_id: str, capability: str) -> str:
    role = require_membership(uid, org_id)
    if capability not in _caps(role):
        raise HTTPException(
            status_code=403,
            detail={
                "error": "forbidden",
                "message": f"requires {capability} capability",
            },
        )
    return role


def can_access_org_workspace(uid: str, ws: dict[str, Any]) -> bool:
    if ws.get("owner_uid") == uid:
        return True
    org_id = (ws.get("org_id") or "").strip()
    if not org_id:
        return False
    return role_of(uid, org_id) is not None


def can_mutate_workspace(uid: str, ws: dict[str, Any]) -> bool:
    if ws.get("owner_uid") == uid:
        return True
    org_id = (ws.get("org_id") or "").strip()
    if not org_id:
        return False
    role = role_of(uid, org_id)
    return role in ("owner", "admin")


def list_members(uid: str, org_id: str) -> list[dict[str, Any]]:
    require_membership(uid, org_id)
    return get_store().list_members(org_id)


def invite(
    actor_uid: str, org_id: str, email: str, role: str = "member"
) -> dict[str, Any]:
    require_capability(actor_uid, org_id, "invite")
    normalized = (email or "").strip().lower()
    if not normalized or "@" not in normalized:
        raise HTTPException(
            status_code=400,
            detail={"error": "invalid_email", "message": "email is required"},
        )
    role_norm = (role or "member").strip().lower()
    if role_norm == "contributor":
        role_norm = "member"
    if role_norm not in ASSIGNABLE_ROLES:
        raise HTTPException(
            status_code=400,
            detail={"error": "invalid_role", "message": "role must be admin or member"},
        )
    actor_role = role_of(actor_uid, org_id) or "member"
    if actor_role == "admin" and role_norm == "admin":
        # admins may invite members only
        raise HTTPException(
            status_code=403,
            detail={"error": "forbidden", "message": "admins cannot assign admin"},
        )
    return get_store().create_invite(
        org_id, email=normalized, role=role_norm, invited_by_uid=actor_uid
    )


def list_org_invites(uid: str, org_id: str) -> list[dict[str, Any]]:
    require_capability(uid, org_id, "invite")
    return get_store().list_org_invites(org_id)


def list_my_pending_invites(uid: str, email: str) -> list[dict[str, Any]]:
    return get_store().list_pending_invites_for_email(email)


def accept_invite(
    uid: str, email: str, display_name: str, invite_id: str, *, avatar_url: str = ""
) -> dict[str, Any]:
    result = get_store().accept_invite(
        invite_id,
        uid=uid,
        email=email,
        display_name=display_name,
        avatar_url=avatar_url,
    )
    if result is None:
        raise HTTPException(
            status_code=404,
            detail={"error": "not_found", "message": "invite not found or not pending"},
        )
    if result.get("error"):
        raise HTTPException(
            status_code=400 if result["error"] != "forbidden" else 403,
            detail={"error": result["error"], "message": result.get("message") or ""},
        )
    return result


def generate_join_code(uid: str, org_id: str) -> dict[str, Any]:
    require_capability(uid, org_id, "transfer")
    code = secrets.token_hex(4).upper()
    return get_store().set_join_code(org_id, code)


def join_by_code(
    uid: str, email: str, display_name: str, code: str, *, avatar_url: str = ""
) -> dict[str, Any]:
    normalized = (code or "").strip().upper()
    if not normalized:
        raise HTTPException(
            status_code=400,
            detail={"error": "invalid_code", "message": "join code is required"},
        )
    result = get_store().join_by_code(
        code=normalized,
        uid=uid,
        email=email,
        display_name=display_name,
        avatar_url=avatar_url,
    )
    if result is None:
        raise HTTPException(
            status_code=404,
            detail={"error": "invalid_code", "message": "invalid join code"},
        )
    return result


def get_org_view(uid: str, org_id: str) -> dict[str, Any]:
    org = get_store().get_org(uid, org_id)
    if org is None:
        raise HTTPException(
            status_code=404,
            detail={"error": "not_found", "message": "organization not found"},
        )
    return org


def list_org_workspaces(uid: str, org_id: str) -> list[dict[str, Any]]:
    require_membership(uid, org_id)
    return get_store().list_org_workspaces(org_id)


def _target_member(org_id: str, target_uid: str) -> dict[str, Any]:
    for row in get_store().list_members(org_id):
        if row.get("uid") == target_uid:
            return row
    raise HTTPException(
        status_code=404,
        detail={"error": "not_found", "message": "member not found"},
    )


def _assert_can_manage_member(
    actor_uid: str, org_id: str, target_uid: str, target_role: str
) -> str:
    actor_role = require_capability(actor_uid, org_id, "manage")
    if actor_uid == target_uid:
        raise HTTPException(
            status_code=403,
            detail={"error": "forbidden", "message": "cannot modify your own membership"},
        )
    role_norm = (target_role or "member").strip().lower()
    if role_norm == "owner":
        raise HTTPException(
            status_code=403,
            detail={"error": "forbidden", "message": "cannot modify organization owner"},
        )
    if actor_role == "admin" and role_norm == "admin":
        raise HTTPException(
            status_code=403,
            detail={"error": "forbidden", "message": "admins cannot modify other admins"},
        )
    if actor_role == "admin" and role_norm != "member":
        raise HTTPException(
            status_code=403,
            detail={"error": "forbidden", "message": "admins can only manage members"},
        )
    return actor_role


def update_member_role(
    actor_uid: str, org_id: str, target_uid: str, role: str
) -> dict[str, Any]:
    target = _target_member(org_id, target_uid)
    _assert_can_manage_member(actor_uid, org_id, target_uid, target.get("role") or "")
    role_norm = (role or "").strip().lower()
    if role_norm == "contributor":
        role_norm = "member"
    if role_norm not in ASSIGNABLE_ROLES:
        raise HTTPException(
            status_code=400,
            detail={"error": "invalid_role", "message": "role must be admin or member"},
        )
    actor_role = role_of(actor_uid, org_id) or "member"
    if actor_role == "admin" and role_norm == "admin":
        raise HTTPException(
            status_code=403,
            detail={"error": "forbidden", "message": "admins cannot assign admin"},
        )
    updated = get_store().update_member_role(org_id, target_uid, role_norm)
    if not updated:
        raise HTTPException(
            status_code=404,
            detail={"error": "not_found", "message": "member not found"},
        )
    return updated


def remove_member(actor_uid: str, org_id: str, target_uid: str) -> dict[str, Any]:
    target = _target_member(org_id, target_uid)
    _assert_can_manage_member(actor_uid, org_id, target_uid, target.get("role") or "")
    ok = get_store().remove_member(org_id, target_uid)
    if not ok:
        raise HTTPException(
            status_code=404,
            detail={"error": "not_found", "message": "member not found"},
        )
    return {"ok": True, "message": "member removed", "id": target_uid}
