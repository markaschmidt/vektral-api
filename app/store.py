"""Domain persistence — Firestore or in-memory.

Provides:
- ``get_store()`` DomainStore (workspaces / orgs / github) for Plan B routers
- ``get_doc`` / ``put_doc`` / ``query_eq`` / ``new_id`` / ``now_iso`` for Plan C services
"""

from __future__ import annotations

import logging
import threading
import uuid
from copy import deepcopy
from typing import Any, Protocol

from app.crypto_tokens import decrypt_secret, encrypt_secret
from app.slugs import slugify, unique_slug
from app.timeutil import now_iso

logger = logging.getLogger("vektral.store")

# Re-export for Plan C services
__all__ = [
    "DomainStore",
    "MemoryStore",
    "FirestoreStore",
    "get_store",
    "reset_memory_store",
    "set_store_override",
    "get_doc",
    "put_doc",
    "query_eq",
    "new_id",
    "now_iso",
    "workspace_to_view",
    "org_to_view",
]


def new_id(prefix: str = "") -> str:
    """Plan C style ids: ``new_id("ws_")`` → ``ws_<hex>``."""
    return f"{prefix}{uuid.uuid4().hex[:16]}"


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


def workspace_to_view(doc: dict[str, Any]) -> dict[str, Any]:
    repo = doc.get("repo_full_name")
    if repo == "":
        repo = None
    linear_project = doc.get("linear_project_id") or None
    if linear_project == "":
        linear_project = None
    linear_team = doc.get("linear_team_id") or None
    if linear_team == "":
        linear_team = None
    status = str(doc.get("site_status") or "").strip()
    if not status:
        status = "ready"
    return {
        "id": doc.get("id") or "",
        "name": doc.get("name") or "",
        "description": doc.get("description") or "",
        "slug": doc.get("slug") or "",
        "repo_full_name": repo,
        "default_branch": doc.get("default_branch") or "main",
        "vektral_branch": doc.get("vektral_branch") or "",
        "storage_prefix": doc.get("storage_prefix") or "",
        "org_id": doc.get("org_id") or None,
        "is_personal": bool(doc.get("is_personal", not bool(doc.get("org_id")))),
        "thumbnail_url": doc.get("thumbnail_url") or None,
        "owner_uid": doc.get("owner_uid") or "",
        "linear_project_id": linear_project,
        "linear_team_id": linear_team,
        "linear_project_name": (doc.get("linear_project_name") or "").strip() or None,
        "linear_project_url": (doc.get("linear_project_url") or "").strip() or None,
        "linear_team_name": (doc.get("linear_team_name") or "").strip() or None,
        "linear_organization_id": (doc.get("linear_organization_id") or "").strip()
        or None,
        "linear_organization_name": (doc.get("linear_organization_name") or "").strip()
        or None,
        "site_ready": status == "ready",
        "site_status": status,
        "site_error": doc.get("site_error") or "",
        "site_commit_sha": doc.get("site_commit_sha") or "",
        "created_at": doc.get("created_at") or "",
        "updated_at": doc.get("updated_at") or "",
    }


def org_to_view(doc: dict[str, Any], *, my_role: str = "member") -> dict[str, Any]:
    if my_role == "owner":
        caps = ["manage", "invite", "create_workspace", "transfer"]
    elif my_role == "admin":
        caps = ["manage", "invite", "create_workspace"]
    else:
        caps = ["create_workspace"] if my_role == "member" else []
    join_code = (doc.get("join_code") or "").strip()
    view = {
        "id": doc.get("id") or "",
        "name": doc.get("name") or "",
        "slug": doc.get("slug") or "",
        "my_role": my_role,
        "my_capabilities": caps,
        "member_count": int(doc.get("member_count") or 1),
        "owner_uid": doc.get("owner_uid") or "",
        "has_join_code": bool(join_code),
        "join_code": "",
        "created_at": doc.get("created_at") or "",
    }
    if my_role == "owner" and join_code:
        view["join_code"] = join_code
    return view


def member_to_view(
    uid: str,
    m: dict[str, Any],
    *,
    role: str | None = None,
    profile: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Merge org member doc with ``users/{uid}`` profile (Firebase SSO fields)."""
    prof = profile or {}
    email = (m.get("email") or prof.get("email") or "").strip()
    display_name = (m.get("display_name") or prof.get("display_name") or uid).strip()
    avatar_url = (m.get("avatar_url") or prof.get("avatar_url") or "").strip()
    return {
        "uid": uid,
        "email": email,
        "display_name": display_name,
        "avatar_url": avatar_url,
        "role": role or m.get("role") or "member",
        "status": m.get("status") or "active",
        "joined_at": m.get("joined_at") or "",
    }


class DomainStore(Protocol):
    def list_workspaces(self, uid: str) -> list[dict[str, Any]]: ...
    def create_workspace(self, uid: str, data: dict[str, Any]) -> dict[str, Any]: ...
    def get_workspace(self, uid: str, workspace_id: str) -> dict[str, Any] | None: ...
    def get_workspace_raw(self, workspace_id: str) -> dict[str, Any] | None: ...
    def update_workspace(
        self, uid: str, workspace_id: str, patch: dict[str, Any]
    ) -> dict[str, Any] | None: ...
    def soft_delete_workspace(self, uid: str, workspace_id: str) -> bool: ...

    def list_orgs(self, uid: str) -> list[dict[str, Any]]: ...
    def create_org(
        self,
        uid: str,
        name: str,
        slug: str | None,
        *,
        email: str = "",
        display_name: str = "",
        avatar_url: str = "",
    ) -> dict[str, Any]: ...
    def get_org(self, uid: str, org_id: str) -> dict[str, Any] | None: ...
    def member_role(self, uid: str, org_id: str) -> str | None: ...
    def list_members(self, org_id: str) -> list[dict[str, Any]]: ...
    def add_member(
        self,
        org_id: str,
        uid: str,
        *,
        role: str,
        email: str = "",
        display_name: str = "",
        avatar_url: str = "",
    ) -> dict[str, Any]: ...
    def update_member_role(
        self, org_id: str, uid: str, role: str
    ) -> dict[str, Any] | None: ...
    def remove_member(self, org_id: str, uid: str) -> bool: ...
    def create_invite(
        self, org_id: str, *, email: str, role: str, invited_by_uid: str
    ) -> dict[str, Any]: ...
    def list_org_invites(self, org_id: str) -> list[dict[str, Any]]: ...
    def list_pending_invites_for_email(self, email: str) -> list[dict[str, Any]]: ...
    def accept_invite(
        self,
        invite_id: str,
        *,
        uid: str,
        email: str,
        display_name: str,
        avatar_url: str = "",
    ) -> dict[str, Any] | None: ...
    def set_join_code(self, org_id: str, code: str) -> dict[str, Any]: ...
    def join_by_code(
        self,
        *,
        code: str,
        uid: str,
        email: str,
        display_name: str,
        avatar_url: str = "",
    ) -> dict[str, Any] | None: ...
    def list_org_workspaces(self, org_id: str) -> list[dict[str, Any]]: ...

    def github_status(self, uid: str) -> dict[str, Any]: ...
    def github_save_pending(self, state: str, uid: str) -> None: ...
    def github_get_pending(self, state: str) -> dict[str, Any] | None: ...
    def github_complete(
        self,
        state: str,
        *,
        login: str,
        access_token: str,
        scopes: str,
        refresh_token: str = "",
        expires_at: str = "",
    ) -> dict[str, Any] | None: ...
    def github_disconnect(self, uid: str) -> None: ...
    def github_access_token(self, uid: str) -> str: ...
    def github_oauth_secrets(self, uid: str) -> dict[str, str]: ...
    def github_replace_tokens(
        self,
        uid: str,
        *,
        access_token: str,
        refresh_token: str | None = None,
        expires_at: str = "",
    ) -> None: ...

    def linear_status(self, uid: str) -> dict[str, Any]: ...
    def linear_save_pending(self, state: str, uid: str) -> None: ...
    def linear_get_pending(self, state: str) -> dict[str, Any] | None: ...
    def linear_complete(
        self,
        state: str,
        *,
        user_id: str,
        name: str,
        email: str,
        access_token: str,
        refresh_token: str = "",
        expires_at: str = "",
        scopes: str = "",
    ) -> dict[str, Any] | None: ...
    def linear_disconnect(self, uid: str) -> None: ...
    def linear_access_token(self, uid: str) -> str: ...
    def linear_oauth_secrets(self, uid: str) -> dict[str, str]: ...
    def linear_replace_tokens(
        self,
        uid: str,
        *,
        access_token: str,
        refresh_token: str | None = None,
        expires_at: str = "",
    ) -> None: ...
    def linear_set_defaults(
        self, uid: str, *, organization_id: str = "", team_id: str = ""
    ) -> dict[str, Any] | None: ...

    # raw collection access (Plan C)
    def raw_get(self, collection: str, doc_id: str) -> dict[str, Any] | None: ...
    def raw_put(self, collection: str, doc_id: str, data: dict[str, Any]) -> None: ...
    def raw_query_eq(
        self, collection: str, field: str, value: Any
    ) -> list[dict[str, Any]]: ...


class MemoryStore:
    """Process-local store for tests / SA-less local CRUD demos."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        # Generic collections for Plan C (panes, jobs, previews, …)
        self.collections: dict[str, dict[str, dict[str, Any]]] = {}
        self.memberships: dict[str, str] = {}  # f"{org}:{uid}" -> role
        self.github: dict[str, dict[str, Any]] = {}
        self.pending: dict[str, dict[str, Any]] = {}
        self.linear: dict[str, dict[str, Any]] = {}
        self.linear_pending: dict[str, dict[str, Any]] = {}

    def raw_get(self, collection: str, doc_id: str) -> dict[str, Any] | None:
        with self._lock:
            doc = self.collections.get(collection, {}).get(doc_id)
            return deepcopy(doc) if doc else None

    def raw_put(self, collection: str, doc_id: str, data: dict[str, Any]) -> None:
        with self._lock:
            self.collections.setdefault(collection, {})[doc_id] = deepcopy(data)

    def raw_query_eq(
        self, collection: str, field: str, value: Any
    ) -> list[dict[str, Any]]:
        with self._lock:
            out = []
            for doc in self.collections.get(collection, {}).values():
                if doc.get(field) == value:
                    out.append(deepcopy(doc))
            return out

    def list_workspaces(self, uid: str) -> list[dict[str, Any]]:
        seen: set[str] = set()
        rows: list[dict[str, Any]] = []
        for w in self.raw_query_eq("workspaces", "owner_uid", uid):
            if w.get("deleted_at") or w.get("deleted"):
                continue
            wid = w.get("id") or ""
            if wid in seen:
                continue
            seen.add(wid)
            rows.append(workspace_to_view(w))
        with self._lock:
            org_ids = [
                key.split(":", 1)[0]
                for key, _role in self.memberships.items()
                if key.endswith(f":{uid}")
            ]
        for org_id in org_ids:
            for w in self.raw_query_eq("workspaces", "org_id", org_id):
                if w.get("deleted_at") or w.get("deleted"):
                    continue
                wid = w.get("id") or ""
                if wid in seen:
                    continue
                seen.add(wid)
                rows.append(workspace_to_view(w))
        rows.sort(key=lambda r: r.get("created_at") or "", reverse=True)
        return rows

    def create_workspace(self, uid: str, data: dict[str, Any]) -> dict[str, Any]:
        wid = data.get("id") or new_id("ws_")
        now = now_iso()
        name = (data.get("name") or "Workspace").strip()
        vektral = data.get("vektral_branch") or f"vektral/{wid[:40]}"
        doc = {
            "id": wid,
            "owner_uid": uid,
            "org_id": data.get("org_id") or "",
            "name": name,
            "description": (data.get("description") or "").strip(),
            "slug": unique_slug(name),
            "repo_full_name": data.get("repo_full_name") or "",
            "default_branch": data.get("default_branch") or "main",
            "vektral_branch": vektral,
            "storage_prefix": f"users/{uid}/workspaces/{wid}",
            "is_personal": not bool(data.get("org_id")),
            "thumbnail_url": data.get("thumbnail_url") or None,
            "linear_project_id": (data.get("linear_project_id") or "").strip(),
            "linear_team_id": (data.get("linear_team_id") or "").strip(),
            "linear_project_name": (data.get("linear_project_name") or "").strip(),
            "linear_project_url": (data.get("linear_project_url") or "").strip(),
            "linear_team_name": (data.get("linear_team_name") or "").strip(),
            "linear_organization_id": (data.get("linear_organization_id") or "").strip(),
            "linear_organization_name": (data.get("linear_organization_name") or "").strip(),
            "site_status": data.get("site_status") or ("seeding" if data.get("repo_full_name") else "ready"),
            "site_ready": bool(data.get("site_ready", not bool(data.get("repo_full_name")))),
            "site_error": data.get("site_error") or "",
            "site_commit_sha": data.get("site_commit_sha") or "",
            "created_at": now,
            "updated_at": now,
            "deleted_at": None,
            "deleted": False,
        }
        self.raw_put("workspaces", wid, doc)
        return workspace_to_view(doc)

    def get_workspace_raw(self, workspace_id: str) -> dict[str, Any] | None:
        w = self.raw_get("workspaces", workspace_id)
        if not w or w.get("deleted_at") or w.get("deleted"):
            return None
        return w

    def get_workspace(self, uid: str, workspace_id: str) -> dict[str, Any] | None:
        w = self.get_workspace_raw(workspace_id)
        if not w:
            return None
        if w.get("owner_uid") == uid:
            return workspace_to_view(w)
        org_id = (w.get("org_id") or "").strip()
        if org_id and self.member_role(uid, org_id):
            return workspace_to_view(w)
        return None

    def _can_mutate_workspace(self, uid: str, w: dict[str, Any]) -> bool:
        if w.get("owner_uid") == uid:
            return True
        org_id = (w.get("org_id") or "").strip()
        if not org_id:
            return False
        return self.member_role(uid, org_id) in ("owner", "admin")

    def update_workspace(
        self, uid: str, workspace_id: str, patch: dict[str, Any]
    ) -> dict[str, Any] | None:
        w = self.get_workspace_raw(workspace_id)
        if not w or not self._can_mutate_workspace(uid, w):
            return None
        for key in (
            "name",
            "description",
            "repo_full_name",
            "default_branch",
            "thumbnail_url",
            "vektral_branch",
            "org_id",
            "linear_project_id",
            "linear_team_id",
            "linear_project_name",
            "linear_project_url",
            "linear_team_name",
            "linear_organization_id",
            "linear_organization_name",
            "site_status",
            "site_ready",
            "site_error",
            "site_commit_sha",
        ):
            if key in patch and patch[key] is not None:
                w[key] = patch[key]
        if "name" in patch and patch["name"]:
            w["slug"] = slugify(patch["name"])
        w["updated_at"] = now_iso()
        self.raw_put("workspaces", workspace_id, w)
        return workspace_to_view(w)

    def soft_delete_workspace(self, uid: str, workspace_id: str) -> bool:
        w = self.get_workspace_raw(workspace_id)
        if not w or not self._can_mutate_workspace(uid, w):
            return False
        now = now_iso()
        w["deleted_at"] = now
        w["deleted"] = True
        w["updated_at"] = now
        self.raw_put("workspaces", workspace_id, w)
        return True

    def list_orgs(self, uid: str) -> list[dict[str, Any]]:
        with self._lock:
            out: list[dict[str, Any]] = []
            for org_id, org in self.collections.get("orgs", {}).items():
                role = self.memberships.get(f"{org_id}:{uid}")
                if role:
                    o = deepcopy(org)
                    o["id"] = org_id
                    out.append(org_to_view(o, my_role=role))
            out.sort(key=lambda r: r.get("created_at") or "", reverse=True)
            return out

    def create_org(
        self,
        uid: str,
        name: str,
        slug: str | None,
        *,
        email: str = "",
        display_name: str = "",
        avatar_url: str = "",
    ) -> dict[str, Any]:
        oid = _new_id("org")
        now = now_iso()
        clean_name = name.strip()
        s = slugify(slug or clean_name) if (slug or clean_name) else unique_slug("org")
        with self._lock:
            existing = {o.get("slug") for o in self.collections.get("orgs", {}).values()}
            if s in existing:
                s = unique_slug(s)
        doc = {
            "id": oid,
            "name": clean_name,
            "slug": s,
            "owner_uid": uid,
            "member_count": 1,
            "created_at": now,
        }
        self.raw_put("orgs", oid, doc)
        with self._lock:
            self.memberships[f"{oid}:{uid}"] = "owner"
        self.raw_put(
            "org_memberships",
            f"{uid}:{oid}",
            {"uid": uid, "org_id": oid, "role": "owner", "joined_at": now},
        )
        self.raw_put(
            "org_members",
            f"{oid}:{uid}",
            {
                "uid": uid,
                "org_id": oid,
                "role": "owner",
                "email": email or f"{uid}@dev.local",
                "display_name": display_name or uid,
                "avatar_url": avatar_url,
                "joined_at": now,
                "status": "active",
            },
        )
        return org_to_view(doc, my_role="owner")

    def get_org(self, uid: str, org_id: str) -> dict[str, Any] | None:
        with self._lock:
            role = self.memberships.get(f"{org_id}:{uid}")
            if not role:
                return None
        org = self.raw_get("orgs", org_id)
        if not org:
            return None
        return org_to_view(org, my_role=role)

    def member_role(self, uid: str, org_id: str) -> str | None:
        with self._lock:
            return self.memberships.get(f"{org_id}:{uid}")

    def list_members(self, org_id: str) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        with self._lock:
            for key, role in self.memberships.items():
                if not key.startswith(f"{org_id}:"):
                    continue
                member_uid = key.split(":", 1)[1]
                meta = self.raw_get("org_members", f"{org_id}:{member_uid}") or {}
                rows.append(
                    member_to_view(member_uid, meta, role=role)
                )
        rows.sort(key=lambda r: (r.get("role") != "owner", r.get("joined_at") or ""))
        return rows

    def add_member(
        self,
        org_id: str,
        uid: str,
        *,
        role: str,
        email: str = "",
        display_name: str = "",
        avatar_url: str = "",
    ) -> dict[str, Any]:
        now = now_iso()
        with self._lock:
            existing = self.memberships.get(f"{org_id}:{uid}")
            if existing:
                return {
                    "uid": uid,
                    "role": existing,
                    "status": "active",
                    "already_member": True,
                }
            self.memberships[f"{org_id}:{uid}"] = role
        self.raw_put(
            "org_members",
            f"{org_id}:{uid}",
            {
                "uid": uid,
                "org_id": org_id,
                "role": role,
                "email": email,
                "display_name": display_name or uid,
                "avatar_url": avatar_url,
                "joined_at": now,
                "status": "active",
            },
        )
        self.raw_put(
            "org_memberships",
            f"{uid}:{org_id}",
            {"uid": uid, "org_id": org_id, "role": role, "joined_at": now},
        )
        org = self.raw_get("orgs", org_id)
        if org:
            org["member_count"] = int(org.get("member_count") or 0) + 1
            self.raw_put("orgs", org_id, org)
        return {
            "uid": uid,
            "role": role,
            "status": "active",
            "already_member": False,
        }

    def update_member_role(
        self, org_id: str, uid: str, role: str
    ) -> dict[str, Any] | None:
        key = f"{org_id}:{uid}"
        with self._lock:
            if key not in self.memberships:
                return None
            self.memberships[key] = role
        meta = self.raw_get("org_members", f"{org_id}:{uid}") or {}
        meta["role"] = role
        self.raw_put("org_members", f"{org_id}:{uid}", meta)
        membership = self.raw_get("org_memberships", f"{uid}:{org_id}") or {}
        membership["role"] = role
        self.raw_put("org_memberships", f"{uid}:{org_id}", membership)
        return member_to_view(uid, meta, role=role)

    def remove_member(self, org_id: str, uid: str) -> bool:
        key = f"{org_id}:{uid}"
        with self._lock:
            if key not in self.memberships:
                return False
            del self.memberships[key]
            self.collections.get("org_members", {}).pop(f"{org_id}:{uid}", None)
            self.collections.get("org_memberships", {}).pop(f"{uid}:{org_id}", None)
        org = self.raw_get("orgs", org_id)
        if org:
            org["member_count"] = max(0, int(org.get("member_count") or 1) - 1)
            self.raw_put("orgs", org_id, org)
        return True

    def create_invite(
        self, org_id: str, *, email: str, role: str, invited_by_uid: str
    ) -> dict[str, Any]:
        org = self.raw_get("orgs", org_id)
        if not org:
            raise KeyError("org missing")
        for inv in self.raw_query_eq("org_invites", "org_id", org_id):
            if (
                inv.get("email") == email
                and inv.get("status") == "pending"
            ):
                return {
                    "id": inv.get("id") or "",
                    "email": email,
                    "role": inv.get("role") or role,
                    "status": "pending",
                    "org_id": org_id,
                    "org_name": org.get("name") or "",
                    "created_at": inv.get("created_at") or "",
                    "duplicate": True,
                }
        iid = _new_id("inv")
        now = now_iso()
        doc = {
            "id": iid,
            "org_id": org_id,
            "org_name": org.get("name") or "",
            "email": email,
            "role": role,
            "status": "pending",
            "invited_by_uid": invited_by_uid,
            "created_at": now,
        }
        self.raw_put("org_invites", iid, doc)
        return {
            "id": iid,
            "email": email,
            "role": role,
            "status": "pending",
            "org_id": org_id,
            "org_name": org.get("name") or "",
            "created_at": now,
            "duplicate": False,
        }

    def list_org_invites(self, org_id: str) -> list[dict[str, Any]]:
        rows = []
        for inv in self.raw_query_eq("org_invites", "org_id", org_id):
            rows.append(
                {
                    "id": inv.get("id") or "",
                    "email": inv.get("email") or "",
                    "role": inv.get("role") or "member",
                    "status": inv.get("status") or "pending",
                    "org_id": org_id,
                    "org_name": inv.get("org_name") or "",
                    "created_at": inv.get("created_at") or "",
                }
            )
        rows.sort(key=lambda r: r.get("created_at") or "", reverse=True)
        return rows

    def list_pending_invites_for_email(self, email: str) -> list[dict[str, Any]]:
        normalized = (email or "").strip().lower()
        if not normalized:
            return []
        rows = []
        for inv in self.collections.get("org_invites", {}).values():
            if inv.get("email") == normalized and inv.get("status") == "pending":
                rows.append(
                    {
                        "id": inv.get("id") or "",
                        "email": inv.get("email") or "",
                        "role": inv.get("role") or "member",
                        "status": "pending",
                        "org_id": inv.get("org_id") or "",
                        "org_name": inv.get("org_name") or "",
                        "created_at": inv.get("created_at") or "",
                    }
                )
        rows.sort(key=lambda r: r.get("created_at") or "", reverse=True)
        return rows

    def accept_invite(
        self,
        invite_id: str,
        *,
        uid: str,
        email: str,
        display_name: str,
        avatar_url: str = "",
    ) -> dict[str, Any] | None:
        inv = self.raw_get("org_invites", invite_id)
        if not inv or inv.get("status") != "pending":
            return None
        if (email or "").strip().lower() != (inv.get("email") or "").strip().lower():
            return {
                "error": "forbidden",
                "message": "invite email does not match your profile",
            }
        org_id = inv.get("org_id") or ""
        role = inv.get("role") or "member"
        member = self.add_member(
            org_id,
            uid,
            role=role,
            email=email,
            display_name=display_name,
            avatar_url=avatar_url,
        )
        inv["status"] = "accepted"
        self.raw_put("org_invites", invite_id, inv)
        org = self.raw_get("orgs", org_id) or {}
        return {
            "ok": True,
            "message": "already a member" if member.get("already_member") else f"joined {org.get('name') or 'org'}",
            "id": org_id,
            "org": org_to_view(org, my_role=self.member_role(uid, org_id) or role)
            if org
            else None,
        }

    def set_join_code(self, org_id: str, code: str) -> dict[str, Any]:
        org = self.raw_get("orgs", org_id)
        if not org:
            raise KeyError("org missing")
        old = (org.get("join_code") or "").strip().upper()
        if old:
            with self._lock:
                self.collections.get("org_join_codes", {}).pop(old, None)
        org["join_code"] = code
        self.raw_put("orgs", org_id, org)
        self.raw_put(
            "org_join_codes",
            code,
            {
                "code": code,
                "org_id": org_id,
                "org_name": org.get("name") or "",
                "created_at": now_iso(),
            },
        )
        return {
            "ok": True,
            "message": f"join code ready: {code}",
            "id": org_id,
            "join_code": code,
            "org": org_to_view(org, my_role="owner"),
        }

    def join_by_code(
        self,
        *,
        code: str,
        uid: str,
        email: str,
        display_name: str,
        avatar_url: str = "",
    ) -> dict[str, Any] | None:
        idx = self.raw_get("org_join_codes", code)
        if not idx:
            return None
        org_id = idx.get("org_id") or ""
        org = self.raw_get("orgs", org_id)
        if not org:
            return None
        member = self.add_member(
            org_id,
            uid,
            role="member",
            email=email,
            display_name=display_name,
            avatar_url=avatar_url,
        )
        role = self.member_role(uid, org_id) or "member"
        return {
            "ok": True,
            "message": "already a member"
            if member.get("already_member")
            else f"joined {org.get('name') or 'org'}",
            "id": org_id,
            "org": org_to_view(org, my_role=role),
        }

    def list_org_workspaces(self, org_id: str) -> list[dict[str, Any]]:
        rows = []
        for w in self.raw_query_eq("workspaces", "org_id", org_id):
            if w.get("deleted_at") or w.get("deleted"):
                continue
            rows.append(workspace_to_view(w))
        rows.sort(key=lambda r: r.get("created_at") or "", reverse=True)
        return rows

    def github_status(self, uid: str) -> dict[str, Any]:
        with self._lock:
            g = self.github.get(uid) or {}
            return {
                "connected": bool(g.get("connected")),
                "github_login": g.get("login") or "",
                "connected_at": g.get("connected_at") or "",
            }

    def github_save_pending(self, state: str, uid: str) -> None:
        with self._lock:
            self.pending[state] = {
                "state": state,
                "uid": uid,
                "status": "pending",
                "created_at": now_iso(),
            }

    def github_get_pending(self, state: str) -> dict[str, Any] | None:
        with self._lock:
            p = self.pending.get(state)
            return deepcopy(p) if p else None

    def github_complete(
        self,
        state: str,
        *,
        login: str,
        access_token: str,
        scopes: str,
        refresh_token: str = "",
        expires_at: str = "",
    ) -> dict[str, Any] | None:
        with self._lock:
            pending = self.pending.get(state)
            if not pending or pending.get("status") != "pending":
                return None
            uid = pending["uid"]
            now = now_iso()
            enc = encrypt_secret(access_token)
            self.github[uid] = {
                "connected": True,
                "login": login,
                "access_token_encrypted": enc,
                "refresh_token_encrypted": encrypt_secret(refresh_token)
                if refresh_token
                else "",
                "expires_at": expires_at,
                "scopes": scopes,
                "connected_at": now,
            }
            # Plan C also looks at users_github
            self.collections.setdefault("users_github", {})[uid] = {
                "uid": uid,
                "access_token": access_token,
                "login": login,
                "connected_at": now,
            }
            pending["status"] = "completed"
            pending["github_login"] = login
            return self.github_status(uid)

    def github_disconnect(self, uid: str) -> None:
        with self._lock:
            self.github.pop(uid, None)
            self.collections.get("users_github", {}).pop(uid, None)

    def github_access_token(self, uid: str) -> str:
        return self.github_oauth_secrets(uid).get("access_token") or ""

    def github_oauth_secrets(self, uid: str) -> dict[str, str]:
        with self._lock:
            g = self.github.get(uid) or {}
            return {
                "access_token": decrypt_secret(g.get("access_token_encrypted") or ""),
                "refresh_token": decrypt_secret(g.get("refresh_token_encrypted") or ""),
                "expires_at": g.get("expires_at") or "",
            }

    def github_replace_tokens(
        self,
        uid: str,
        *,
        access_token: str,
        refresh_token: str | None = None,
        expires_at: str = "",
    ) -> None:
        with self._lock:
            g = self.github.get(uid)
            if not g:
                return
            g["access_token_encrypted"] = encrypt_secret(access_token)
            if refresh_token is not None:
                g["refresh_token_encrypted"] = (
                    encrypt_secret(refresh_token) if refresh_token else ""
                )
            if expires_at:
                g["expires_at"] = expires_at
            self.collections.setdefault("users_github", {})[uid] = {
                "uid": uid,
                "access_token": access_token,
                "login": g.get("login") or "",
                "connected_at": g.get("connected_at") or now_iso(),
            }

    def linear_status(self, uid: str) -> dict[str, Any]:
        with self._lock:
            g = self.linear.get(uid) or {}
            return {
                "connected": bool(g.get("connected")),
                "linear_user_id": g.get("user_id") or "",
                "linear_name": g.get("name") or "",
                "linear_email": g.get("email") or "",
                "connected_at": g.get("connected_at") or "",
                "default_organization_id": g.get("default_organization_id") or "",
                "default_team_id": g.get("default_team_id") or "",
            }

    def linear_save_pending(self, state: str, uid: str) -> None:
        with self._lock:
            self.linear_pending[state] = {
                "state": state,
                "uid": uid,
                "status": "pending",
                "created_at": now_iso(),
            }

    def linear_get_pending(self, state: str) -> dict[str, Any] | None:
        with self._lock:
            p = self.linear_pending.get(state)
            return deepcopy(p) if p else None

    def linear_complete(
        self,
        state: str,
        *,
        user_id: str,
        name: str,
        email: str,
        access_token: str,
        refresh_token: str = "",
        expires_at: str = "",
        scopes: str = "",
    ) -> dict[str, Any] | None:
        with self._lock:
            pending = self.linear_pending.get(state)
            if not pending or pending.get("status") != "pending":
                return None
            uid = pending["uid"]
            now = now_iso()
            prev = self.linear.get(uid) or {}
            self.linear[uid] = {
                "connected": True,
                "user_id": user_id,
                "name": name,
                "email": email,
                "access_token_encrypted": encrypt_secret(access_token),
                "refresh_token_encrypted": encrypt_secret(refresh_token)
                if refresh_token
                else "",
                "expires_at": expires_at,
                "scopes": scopes,
                "connected_at": now,
                "default_organization_id": prev.get("default_organization_id") or "",
                "default_team_id": prev.get("default_team_id") or "",
            }
            self.collections.setdefault("users_linear", {})[uid] = {
                "uid": uid,
                "access_token": access_token,
                "user_id": user_id,
                "name": name,
                "connected_at": now,
            }
            pending["status"] = "completed"
            pending["linear_name"] = name
            return self.linear_status(uid)

    def linear_disconnect(self, uid: str) -> None:
        with self._lock:
            self.linear.pop(uid, None)
            self.collections.get("users_linear", {}).pop(uid, None)

    def linear_access_token(self, uid: str) -> str:
        return self.linear_oauth_secrets(uid).get("access_token") or ""

    def linear_oauth_secrets(self, uid: str) -> dict[str, str]:
        with self._lock:
            g = self.linear.get(uid) or {}
            return {
                "access_token": decrypt_secret(g.get("access_token_encrypted") or ""),
                "refresh_token": decrypt_secret(g.get("refresh_token_encrypted") or ""),
                "expires_at": g.get("expires_at") or "",
            }

    def linear_replace_tokens(
        self,
        uid: str,
        *,
        access_token: str,
        refresh_token: str | None = None,
        expires_at: str = "",
    ) -> None:
        with self._lock:
            g = self.linear.get(uid)
            if not g:
                return
            g["access_token_encrypted"] = encrypt_secret(access_token)
            if refresh_token is not None:
                g["refresh_token_encrypted"] = (
                    encrypt_secret(refresh_token) if refresh_token else ""
                )
            if expires_at:
                g["expires_at"] = expires_at
            self.collections.setdefault("users_linear", {})[uid] = {
                "uid": uid,
                "access_token": access_token,
                "user_id": g.get("user_id") or "",
                "name": g.get("name") or "",
                "connected_at": g.get("connected_at") or now_iso(),
            }

    def linear_set_defaults(
        self, uid: str, *, organization_id: str = "", team_id: str = ""
    ) -> dict[str, Any] | None:
        with self._lock:
            g = self.linear.get(uid)
            if not g or not g.get("connected"):
                return None
            g["default_organization_id"] = organization_id
            g["default_team_id"] = team_id
            return self.linear_status(uid)


class FirestoreStore:
    """Firestore-backed domain store (Admin SDK)."""

    def __init__(self, db: Any) -> None:
        self._db = db

    def raw_get(self, collection: str, doc_id: str) -> dict[str, Any] | None:
        snap = self._db.collection(collection).document(doc_id).get()
        if not snap.exists:
            return None
        data = snap.to_dict() or {}
        data.setdefault("id", snap.id)
        return data

    def raw_put(self, collection: str, doc_id: str, data: dict[str, Any]) -> None:
        payload = {**data, "id": doc_id}
        self._db.collection(collection).document(doc_id).set(payload, merge=True)

    def raw_query_eq(
        self, collection: str, field: str, value: Any
    ) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for snap in self._db.collection(collection).where(field, "==", value).stream():
            data = snap.to_dict() or {}
            data["id"] = snap.id
            out.append(data)
        return out

    def list_workspaces(self, uid: str) -> list[dict[str, Any]]:
        seen: set[str] = set()
        rows: list[dict[str, Any]] = []
        for data in self.raw_query_eq("workspaces", "owner_uid", uid):
            if data.get("deleted_at") or data.get("deleted"):
                continue
            wid = data.get("id") or ""
            if wid in seen:
                continue
            seen.add(wid)
            rows.append(workspace_to_view(data))
        mems = (
            self._db.collection("users")
            .document(uid)
            .collection("org_memberships")
            .stream()
        )
        for msnap in mems:
            org_id = msnap.id
            for data in self.raw_query_eq("workspaces", "org_id", org_id):
                if data.get("deleted_at") or data.get("deleted"):
                    continue
                wid = data.get("id") or ""
                if wid in seen:
                    continue
                seen.add(wid)
                rows.append(workspace_to_view(data))
        rows.sort(key=lambda r: r.get("created_at") or "", reverse=True)
        return rows

    def create_workspace(self, uid: str, data: dict[str, Any]) -> dict[str, Any]:
        wid = data.get("id") or new_id("ws_")
        now = now_iso()
        name = (data.get("name") or "Workspace").strip()
        vektral = data.get("vektral_branch") or f"vektral/{wid[:40]}"
        doc = {
            "id": wid,
            "owner_uid": uid,
            "org_id": data.get("org_id") or "",
            "name": name,
            "description": (data.get("description") or "").strip(),
            "slug": unique_slug(name),
            "repo_full_name": data.get("repo_full_name") or "",
            "default_branch": data.get("default_branch") or "main",
            "vektral_branch": vektral,
            "storage_prefix": f"users/{uid}/workspaces/{wid}",
            "is_personal": not bool(data.get("org_id")),
            "thumbnail_url": data.get("thumbnail_url") or None,
            "linear_project_id": (data.get("linear_project_id") or "").strip(),
            "linear_team_id": (data.get("linear_team_id") or "").strip(),
            "linear_project_name": (data.get("linear_project_name") or "").strip(),
            "linear_project_url": (data.get("linear_project_url") or "").strip(),
            "linear_team_name": (data.get("linear_team_name") or "").strip(),
            "linear_organization_id": (data.get("linear_organization_id") or "").strip(),
            "linear_organization_name": (data.get("linear_organization_name") or "").strip(),
            "site_status": data.get("site_status") or ("seeding" if data.get("repo_full_name") else "ready"),
            "site_ready": bool(data.get("site_ready", not bool(data.get("repo_full_name")))),
            "site_error": data.get("site_error") or "",
            "site_commit_sha": data.get("site_commit_sha") or "",
            "created_at": now,
            "updated_at": now,
            "deleted_at": None,
            "deleted": False,
        }
        self.raw_put("workspaces", wid, doc)
        self._db.collection("users").document(uid).collection("workspaces").document(wid).set(
            {"workspace_id": wid, "name": name, "created_at": now},
            merge=True,
        )
        return workspace_to_view(doc)

    def get_workspace_raw(self, workspace_id: str) -> dict[str, Any] | None:
        data = self.raw_get("workspaces", workspace_id)
        if not data or data.get("deleted_at") or data.get("deleted"):
            return None
        return data

    def get_workspace(self, uid: str, workspace_id: str) -> dict[str, Any] | None:
        data = self.get_workspace_raw(workspace_id)
        if not data:
            return None
        if data.get("owner_uid") == uid:
            return workspace_to_view(data)
        org_id = (data.get("org_id") or "").strip()
        if org_id and self.member_role(uid, org_id):
            return workspace_to_view(data)
        return None

    def _can_mutate_workspace(self, uid: str, data: dict[str, Any]) -> bool:
        if data.get("owner_uid") == uid:
            return True
        org_id = (data.get("org_id") or "").strip()
        if not org_id:
            return False
        return self.member_role(uid, org_id) in ("owner", "admin")

    def update_workspace(
        self, uid: str, workspace_id: str, patch: dict[str, Any]
    ) -> dict[str, Any] | None:
        data = self.get_workspace_raw(workspace_id)
        if not data or not self._can_mutate_workspace(uid, data):
            return None
        updates: dict[str, Any] = {"updated_at": now_iso()}
        for key in (
            "name",
            "description",
            "repo_full_name",
            "default_branch",
            "thumbnail_url",
            "vektral_branch",
            "org_id",
            "linear_project_id",
            "linear_team_id",
            "linear_project_name",
            "linear_project_url",
            "linear_team_name",
            "linear_organization_id",
            "linear_organization_name",
            "site_status",
            "site_ready",
            "site_error",
            "site_commit_sha",
        ):
            if key in patch and patch[key] is not None:
                updates[key] = patch[key]
        if "org_id" in updates:
            updates["is_personal"] = not bool(updates.get("org_id"))
        if "name" in updates:
            updates["slug"] = slugify(updates["name"])
        data.update(updates)
        self.raw_put("workspaces", workspace_id, data)
        return workspace_to_view(data)

    def soft_delete_workspace(self, uid: str, workspace_id: str) -> bool:
        data = self.get_workspace_raw(workspace_id)
        if not data or not self._can_mutate_workspace(uid, data):
            return False
        now = now_iso()
        data["deleted_at"] = now
        data["deleted"] = True
        data["updated_at"] = now
        self.raw_put("workspaces", workspace_id, data)
        return True

    def list_orgs(self, uid: str) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        mems = (
            self._db.collection("users")
            .document(uid)
            .collection("org_memberships")
            .stream()
        )
        for msnap in mems:
            m = msnap.to_dict() or {}
            org_id = msnap.id
            data = self.raw_get("orgs", org_id)
            if not data:
                continue
            rows.append(org_to_view(data, my_role=m.get("role") or "member"))
        rows.sort(key=lambda r: r.get("created_at") or "", reverse=True)
        return rows

    def create_org(
        self,
        uid: str,
        name: str,
        slug: str | None,
        *,
        email: str = "",
        display_name: str = "",
        avatar_url: str = "",
    ) -> dict[str, Any]:
        oid = _new_id("org")
        now = now_iso()
        clean_name = name.strip()
        s = slugify(slug or clean_name) if (slug or clean_name) else unique_slug("org")
        profile = self._user_public_profile(uid)
        doc = {
            "id": oid,
            "name": clean_name,
            "slug": s,
            "owner_uid": uid,
            "member_count": 1,
            "created_at": now,
        }
        self.raw_put("orgs", oid, doc)
        self._db.collection("orgs").document(oid).collection("members").document(uid).set(
            {
                "role": "owner",
                "status": "active",
                "joined_at": now,
                "email": email or profile.get("email") or "",
                "display_name": display_name or profile.get("display_name") or uid,
                "avatar_url": avatar_url or profile.get("avatar_url") or "",
            }
        )
        self._db.collection("users").document(uid).collection("org_memberships").document(
            oid
        ).set({"role": "owner", "org_name": clean_name, "joined_at": now})
        try:
            from google.cloud.firestore import Increment

            self._db.collection("users").document(uid).set(
                {"org_count": Increment(1), "updated_at": now}, merge=True
            )
        except Exception:  # noqa: BLE001
            self._db.collection("users").document(uid).set(
                {"org_count": 1, "updated_at": now}, merge=True
            )
        return org_to_view(doc, my_role="owner")

    def get_org(self, uid: str, org_id: str) -> dict[str, Any] | None:
        msnap = (
            self._db.collection("users")
            .document(uid)
            .collection("org_memberships")
            .document(org_id)
            .get()
        )
        if not msnap.exists:
            return None
        data = self.raw_get("orgs", org_id)
        if not data:
            return None
        role = (msnap.to_dict() or {}).get("role") or "member"
        return org_to_view(data, my_role=role)

    def member_role(self, uid: str, org_id: str) -> str | None:
        msnap = (
            self._db.collection("users")
            .document(uid)
            .collection("org_memberships")
            .document(org_id)
            .get()
        )
        if not msnap.exists:
            return None
        return (msnap.to_dict() or {}).get("role") or "member"

    def _user_public_profile(self, uid: str) -> dict[str, str]:
        snap = self._db.collection("users").document(uid).get()
        if not snap.exists:
            return {"email": "", "display_name": "", "avatar_url": ""}
        data = snap.to_dict() or {}
        avatar = (data.get("avatar_url") or data.get("photo_url") or "").strip()
        return {
            "email": (data.get("email") or "").strip(),
            "display_name": (data.get("display_name") or "").strip(),
            "avatar_url": avatar,
        }

    def list_members(self, org_id: str) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for snap in (
            self._db.collection("orgs").document(org_id).collection("members").stream()
        ):
            m = snap.to_dict() or {}
            profile = self._user_public_profile(snap.id)
            rows.append(member_to_view(snap.id, m, profile=profile))
        rows.sort(key=lambda r: (r.get("role") != "owner", r.get("joined_at") or ""))
        return rows

    def add_member(
        self,
        org_id: str,
        uid: str,
        *,
        role: str,
        email: str = "",
        display_name: str = "",
        avatar_url: str = "",
    ) -> dict[str, Any]:
        existing = self.member_role(uid, org_id)
        if existing:
            return {
                "uid": uid,
                "role": existing,
                "status": "active",
                "already_member": True,
            }
        now = now_iso()
        org = self.raw_get("orgs", org_id) or {}
        profile = self._user_public_profile(uid)
        self._db.collection("orgs").document(org_id).collection("members").document(
            uid
        ).set(
            {
                "role": role,
                "status": "active",
                "joined_at": now,
                "email": email or profile.get("email") or "",
                "display_name": display_name or profile.get("display_name") or uid,
                "avatar_url": avatar_url or profile.get("avatar_url") or "",
            }
        )
        self._db.collection("users").document(uid).collection("org_memberships").document(
            org_id
        ).set(
            {
                "role": role,
                "org_name": org.get("name") or "",
                "joined_at": now,
            }
        )
        try:
            from google.cloud.firestore import Increment

            self._db.collection("orgs").document(org_id).set(
                {"member_count": Increment(1)}, merge=True
            )
            self._db.collection("users").document(uid).set(
                {"org_count": Increment(1), "updated_at": now}, merge=True
            )
        except Exception:  # noqa: BLE001
            count = int(org.get("member_count") or 0) + 1
            self._db.collection("orgs").document(org_id).set(
                {"member_count": count}, merge=True
            )
        return {
            "uid": uid,
            "role": role,
            "status": "active",
            "already_member": False,
        }

    def update_member_role(
        self, org_id: str, uid: str, role: str
    ) -> dict[str, Any] | None:
        if not self.member_role(uid, org_id):
            return None
        self._db.collection("orgs").document(org_id).collection("members").document(
            uid
        ).set({"role": role}, merge=True)
        self._db.collection("users").document(uid).collection(
            "org_memberships"
        ).document(org_id).set({"role": role}, merge=True)
        snap = (
            self._db.collection("orgs")
            .document(org_id)
            .collection("members")
            .document(uid)
            .get()
        )
        m = snap.to_dict() or {}
        profile = self._user_public_profile(uid)
        return member_to_view(uid, {**m, "role": role}, profile=profile)

    def remove_member(self, org_id: str, uid: str) -> bool:
        if not self.member_role(uid, org_id):
            return False
        self._db.collection("orgs").document(org_id).collection("members").document(
            uid
        ).delete()
        self._db.collection("users").document(uid).collection(
            "org_memberships"
        ).document(org_id).delete()
        try:
            from google.cloud.firestore import Increment

            self._db.collection("orgs").document(org_id).set(
                {"member_count": Increment(-1)}, merge=True
            )
            self._db.collection("users").document(uid).set(
                {"org_count": Increment(-1), "updated_at": now_iso()}, merge=True
            )
        except Exception:  # noqa: BLE001
            org = self.raw_get("orgs", org_id) or {}
            count = max(0, int(org.get("member_count") or 1) - 1)
            self._db.collection("orgs").document(org_id).set(
                {"member_count": count}, merge=True
            )
        return True

    def create_invite(
        self, org_id: str, *, email: str, role: str, invited_by_uid: str
    ) -> dict[str, Any]:
        org = self.raw_get("orgs", org_id) or {}
        for snap in (
            self._db.collection("orgs").document(org_id).collection("invites").stream()
        ):
            inv = snap.to_dict() or {}
            if inv.get("email") == email and inv.get("status") == "pending":
                return {
                    "id": snap.id,
                    "email": email,
                    "role": inv.get("role") or role,
                    "status": "pending",
                    "org_id": org_id,
                    "org_name": org.get("name") or "",
                    "created_at": inv.get("created_at") or "",
                    "duplicate": True,
                }
        iid = _new_id("inv")
        now = now_iso()
        doc = {
            "id": iid,
            "org_id": org_id,
            "org_name": org.get("name") or "",
            "email": email,
            "role": role,
            "status": "pending",
            "invited_by_uid": invited_by_uid,
            "created_at": now,
        }
        self._db.collection("orgs").document(org_id).collection("invites").document(
            iid
        ).set(doc)
        self._db.collection("invite_index").document(iid).set(doc)
        return {
            "id": iid,
            "email": email,
            "role": role,
            "status": "pending",
            "org_id": org_id,
            "org_name": org.get("name") or "",
            "created_at": now,
            "duplicate": False,
        }

    def list_org_invites(self, org_id: str) -> list[dict[str, Any]]:
        rows = []
        for snap in (
            self._db.collection("orgs").document(org_id).collection("invites").stream()
        ):
            inv = snap.to_dict() or {}
            rows.append(
                {
                    "id": snap.id,
                    "email": inv.get("email") or "",
                    "role": inv.get("role") or "member",
                    "status": inv.get("status") or "pending",
                    "org_id": org_id,
                    "org_name": inv.get("org_name") or "",
                    "created_at": inv.get("created_at") or "",
                }
            )
        rows.sort(key=lambda r: r.get("created_at") or "", reverse=True)
        return rows

    def list_pending_invites_for_email(self, email: str) -> list[dict[str, Any]]:
        normalized = (email or "").strip().lower()
        if not normalized:
            return []
        rows = []
        for snap in (
            self._db.collection("invite_index")
            .where("email", "==", normalized)
            .where("status", "==", "pending")
            .stream()
        ):
            inv = snap.to_dict() or {}
            rows.append(
                {
                    "id": snap.id,
                    "email": inv.get("email") or "",
                    "role": inv.get("role") or "member",
                    "status": "pending",
                    "org_id": inv.get("org_id") or "",
                    "org_name": inv.get("org_name") or "",
                    "created_at": inv.get("created_at") or "",
                }
            )
        rows.sort(key=lambda r: r.get("created_at") or "", reverse=True)
        return rows

    def accept_invite(
        self,
        invite_id: str,
        *,
        uid: str,
        email: str,
        display_name: str,
        avatar_url: str = "",
    ) -> dict[str, Any] | None:
        idx = self._db.collection("invite_index").document(invite_id).get()
        if not idx.exists:
            return None
        inv = idx.to_dict() or {}
        if inv.get("status") != "pending":
            return None
        if (email or "").strip().lower() != (inv.get("email") or "").strip().lower():
            return {
                "error": "forbidden",
                "message": "invite email does not match your profile",
            }
        org_id = inv.get("org_id") or ""
        role = inv.get("role") or "member"
        member = self.add_member(
            org_id,
            uid,
            role=role,
            email=email,
            display_name=display_name,
            avatar_url=avatar_url,
        )
        inv["status"] = "accepted"
        self._db.collection("invite_index").document(invite_id).set(inv, merge=True)
        self._db.collection("orgs").document(org_id).collection("invites").document(
            invite_id
        ).set({"status": "accepted"}, merge=True)
        org = self.raw_get("orgs", org_id) or {}
        return {
            "ok": True,
            "message": "already a member"
            if member.get("already_member")
            else f"joined {org.get('name') or 'org'}",
            "id": org_id,
            "org": org_to_view(org, my_role=self.member_role(uid, org_id) or role)
            if org
            else None,
        }

    def set_join_code(self, org_id: str, code: str) -> dict[str, Any]:
        org = self.raw_get("orgs", org_id)
        if not org:
            raise KeyError("org missing")
        old = (org.get("join_code") or "").strip().upper()
        if old:
            try:
                self._db.collection("org_join_codes").document(old).delete()
            except Exception:  # noqa: BLE001
                pass
        org["join_code"] = code
        self.raw_put("orgs", org_id, org)
        self._db.collection("org_join_codes").document(code).set(
            {
                "code": code,
                "org_id": org_id,
                "org_name": org.get("name") or "",
                "created_at": now_iso(),
            }
        )
        return {
            "ok": True,
            "message": f"join code ready: {code}",
            "id": org_id,
            "join_code": code,
            "org": org_to_view(org, my_role="owner"),
        }

    def join_by_code(
        self,
        *,
        code: str,
        uid: str,
        email: str,
        display_name: str,
        avatar_url: str = "",
    ) -> dict[str, Any] | None:
        snap = self._db.collection("org_join_codes").document(code).get()
        if not snap.exists:
            return None
        idx = snap.to_dict() or {}
        org_id = idx.get("org_id") or ""
        org = self.raw_get("orgs", org_id)
        if not org:
            return None
        member = self.add_member(
            org_id,
            uid,
            role="member",
            email=email,
            display_name=display_name,
            avatar_url=avatar_url,
        )
        role = self.member_role(uid, org_id) or "member"
        return {
            "ok": True,
            "message": "already a member"
            if member.get("already_member")
            else f"joined {org.get('name') or 'org'}",
            "id": org_id,
            "org": org_to_view(org, my_role=role),
        }

    def list_org_workspaces(self, org_id: str) -> list[dict[str, Any]]:
        rows = []
        for data in self.raw_query_eq("workspaces", "org_id", org_id):
            if data.get("deleted_at") or data.get("deleted"):
                continue
            rows.append(workspace_to_view(data))
        rows.sort(key=lambda r: r.get("created_at") or "", reverse=True)
        return rows

    def github_status(self, uid: str) -> dict[str, Any]:
        snap = (
            self._db.collection("users")
            .document(uid)
            .collection("github")
            .document("connection")
            .get()
        )
        if not snap.exists:
            usnap = self._db.collection("users").document(uid).get()
            u = usnap.to_dict() if usnap.exists else {}
            return {
                "connected": bool((u or {}).get("github_connected")),
                "github_login": (u or {}).get("github_login") or "",
                "connected_at": (u or {}).get("github_connected_at") or "",
            }
        g = snap.to_dict() or {}
        return {
            "connected": bool(g.get("connected")),
            "github_login": g.get("login") or "",
            "connected_at": g.get("connected_at") or "",
        }

    def github_save_pending(self, state: str, uid: str) -> None:
        self._db.collection("github_oauth_pending").document(state).set(
            {
                "state": state,
                "uid": uid,
                "status": "pending",
                "created_at": now_iso(),
            }
        )

    def github_get_pending(self, state: str) -> dict[str, Any] | None:
        snap = self._db.collection("github_oauth_pending").document(state).get()
        if not snap.exists:
            return None
        return snap.to_dict()

    def github_complete(
        self,
        state: str,
        *,
        login: str,
        access_token: str,
        scopes: str,
        refresh_token: str = "",
        expires_at: str = "",
    ) -> dict[str, Any] | None:
        pref = self._db.collection("github_oauth_pending").document(state)
        snap = pref.get()
        if not snap.exists:
            return None
        pending = snap.to_dict() or {}
        if pending.get("status") != "pending":
            return None
        uid = pending.get("uid")
        if not uid:
            return None
        now = now_iso()
        enc = encrypt_secret(access_token)
        self._db.collection("users").document(uid).collection("github").document(
            "connection"
        ).set(
            {
                "connected": True,
                "login": login,
                "access_token_encrypted": enc,
                "refresh_token_encrypted": encrypt_secret(refresh_token)
                if refresh_token
                else "",
                "expires_at": expires_at,
                "scopes": scopes,
                "connected_at": now,
            }
        )
        self._db.collection("users").document(uid).set(
            {
                "github_login": login,
                "github_connected": True,
                "github_connected_at": now,
                "updated_at": now,
            },
            merge=True,
        )
        # Plan C helper path
        self.raw_put(
            "users_github",
            uid,
            {"uid": uid, "access_token": access_token, "login": login, "connected_at": now},
        )
        pref.set({"status": "completed", "github_login": login}, merge=True)
        return self.github_status(uid)

    def github_disconnect(self, uid: str) -> None:
        self._db.collection("users").document(uid).collection("github").document(
            "connection"
        ).delete()
        self._db.collection("users").document(uid).set(
            {
                "github_login": "",
                "github_connected": False,
                "github_connected_at": "",
                "updated_at": now_iso(),
            },
            merge=True,
        )
        try:
            self._db.collection("users_github").document(uid).delete()
        except Exception:  # noqa: BLE001
            pass

    def github_access_token(self, uid: str) -> str:
        return self.github_oauth_secrets(uid).get("access_token") or ""

    def github_oauth_secrets(self, uid: str) -> dict[str, str]:
        snap = (
            self._db.collection("users")
            .document(uid)
            .collection("github")
            .document("connection")
            .get()
        )
        if not snap.exists:
            return {"access_token": "", "refresh_token": "", "expires_at": ""}
        g = snap.to_dict() or {}
        return {
            "access_token": decrypt_secret(g.get("access_token_encrypted") or ""),
            "refresh_token": decrypt_secret(g.get("refresh_token_encrypted") or ""),
            "expires_at": g.get("expires_at") or "",
        }

    def github_replace_tokens(
        self,
        uid: str,
        *,
        access_token: str,
        refresh_token: str | None = None,
        expires_at: str = "",
    ) -> None:
        payload: dict[str, Any] = {
            "access_token_encrypted": encrypt_secret(access_token),
        }
        if refresh_token is not None:
            payload["refresh_token_encrypted"] = (
                encrypt_secret(refresh_token) if refresh_token else ""
            )
        if expires_at:
            payload["expires_at"] = expires_at
        self._db.collection("users").document(uid).collection("github").document(
            "connection"
        ).set(payload, merge=True)
        self.raw_put(
            "users_github",
            uid,
            {
                "uid": uid,
                "access_token": access_token,
                "login": (self.github_status(uid).get("github_login") or ""),
                "connected_at": now_iso(),
            },
        )

    def linear_status(self, uid: str) -> dict[str, Any]:
        snap = (
            self._db.collection("users")
            .document(uid)
            .collection("linear")
            .document("connection")
            .get()
        )
        if not snap.exists:
            usnap = self._db.collection("users").document(uid).get()
            u = usnap.to_dict() if usnap.exists else {}
            return {
                "connected": bool((u or {}).get("linear_connected")),
                "linear_user_id": (u or {}).get("linear_user_id") or "",
                "linear_name": (u or {}).get("linear_name") or "",
                "linear_email": (u or {}).get("linear_email") or "",
                "connected_at": (u or {}).get("linear_connected_at") or "",
                "default_organization_id": (u or {}).get("linear_default_organization_id")
                or "",
                "default_team_id": (u or {}).get("linear_default_team_id") or "",
            }
        g = snap.to_dict() or {}
        return {
            "connected": bool(g.get("connected")),
            "linear_user_id": g.get("user_id") or "",
            "linear_name": g.get("name") or "",
            "linear_email": g.get("email") or "",
            "connected_at": g.get("connected_at") or "",
            "default_organization_id": g.get("default_organization_id") or "",
            "default_team_id": g.get("default_team_id") or "",
        }

    def linear_save_pending(self, state: str, uid: str) -> None:
        self._db.collection("linear_oauth_pending").document(state).set(
            {
                "state": state,
                "uid": uid,
                "status": "pending",
                "created_at": now_iso(),
            }
        )

    def linear_get_pending(self, state: str) -> dict[str, Any] | None:
        snap = self._db.collection("linear_oauth_pending").document(state).get()
        if not snap.exists:
            return None
        return snap.to_dict()

    def linear_complete(
        self,
        state: str,
        *,
        user_id: str,
        name: str,
        email: str,
        access_token: str,
        refresh_token: str = "",
        expires_at: str = "",
        scopes: str = "",
    ) -> dict[str, Any] | None:
        pref = self._db.collection("linear_oauth_pending").document(state)
        snap = pref.get()
        if not snap.exists:
            return None
        pending = snap.to_dict() or {}
        if pending.get("status") != "pending":
            return None
        uid = pending.get("uid")
        if not uid:
            return None
        now = now_iso()
        prev_snap = (
            self._db.collection("users")
            .document(uid)
            .collection("linear")
            .document("connection")
            .get()
        )
        prev = prev_snap.to_dict() if prev_snap.exists else {}
        payload = {
            "connected": True,
            "user_id": user_id,
            "name": name,
            "email": email,
            "access_token_encrypted": encrypt_secret(access_token),
            "refresh_token_encrypted": encrypt_secret(refresh_token)
            if refresh_token
            else "",
            "expires_at": expires_at,
            "scopes": scopes,
            "connected_at": now,
            "default_organization_id": (prev or {}).get("default_organization_id") or "",
            "default_team_id": (prev or {}).get("default_team_id") or "",
        }
        self._db.collection("users").document(uid).collection("linear").document(
            "connection"
        ).set(payload)
        self._db.collection("users").document(uid).set(
            {
                "linear_user_id": user_id,
                "linear_name": name,
                "linear_email": email,
                "linear_connected": True,
                "linear_connected_at": now,
                "updated_at": now,
            },
            merge=True,
        )
        self.raw_put(
            "users_linear",
            uid,
            {
                "uid": uid,
                "access_token": access_token,
                "user_id": user_id,
                "name": name,
                "connected_at": now,
            },
        )
        pref.set({"status": "completed", "linear_name": name}, merge=True)
        return self.linear_status(uid)

    def linear_disconnect(self, uid: str) -> None:
        self._db.collection("users").document(uid).collection("linear").document(
            "connection"
        ).delete()
        self._db.collection("users").document(uid).set(
            {
                "linear_user_id": "",
                "linear_name": "",
                "linear_email": "",
                "linear_connected": False,
                "linear_connected_at": "",
                "linear_default_organization_id": "",
                "linear_default_team_id": "",
                "updated_at": now_iso(),
            },
            merge=True,
        )
        try:
            self._db.collection("users_linear").document(uid).delete()
        except Exception:  # noqa: BLE001
            pass

    def linear_access_token(self, uid: str) -> str:
        return self.linear_oauth_secrets(uid).get("access_token") or ""

    def linear_oauth_secrets(self, uid: str) -> dict[str, str]:
        snap = (
            self._db.collection("users")
            .document(uid)
            .collection("linear")
            .document("connection")
            .get()
        )
        if not snap.exists:
            return {"access_token": "", "refresh_token": "", "expires_at": ""}
        g = snap.to_dict() or {}
        return {
            "access_token": decrypt_secret(g.get("access_token_encrypted") or ""),
            "refresh_token": decrypt_secret(g.get("refresh_token_encrypted") or ""),
            "expires_at": g.get("expires_at") or "",
        }

    def linear_replace_tokens(
        self,
        uid: str,
        *,
        access_token: str,
        refresh_token: str | None = None,
        expires_at: str = "",
    ) -> None:
        payload: dict[str, Any] = {
            "access_token_encrypted": encrypt_secret(access_token),
        }
        if refresh_token is not None:
            payload["refresh_token_encrypted"] = (
                encrypt_secret(refresh_token) if refresh_token else ""
            )
        if expires_at:
            payload["expires_at"] = expires_at
        self._db.collection("users").document(uid).collection("linear").document(
            "connection"
        ).set(payload, merge=True)
        status = self.linear_status(uid)
        self.raw_put(
            "users_linear",
            uid,
            {
                "uid": uid,
                "access_token": access_token,
                "user_id": status.get("linear_user_id") or "",
                "name": status.get("linear_name") or "",
                "connected_at": now_iso(),
            },
        )

    def linear_set_defaults(
        self, uid: str, *, organization_id: str = "", team_id: str = ""
    ) -> dict[str, Any] | None:
        ref = (
            self._db.collection("users")
            .document(uid)
            .collection("linear")
            .document("connection")
        )
        snap = ref.get()
        if not snap.exists:
            return None
        g = snap.to_dict() or {}
        if not g.get("connected"):
            return None
        ref.set(
            {
                "default_organization_id": organization_id,
                "default_team_id": team_id,
            },
            merge=True,
        )
        self._db.collection("users").document(uid).set(
            {
                "linear_default_organization_id": organization_id,
                "linear_default_team_id": team_id,
                "updated_at": now_iso(),
            },
            merge=True,
        )
        return self.linear_status(uid)


_memory_singleton: MemoryStore | None = None
_store_override: DomainStore | None = None


def reset_memory_store() -> MemoryStore:
    global _memory_singleton
    _memory_singleton = MemoryStore()
    return _memory_singleton


def set_store_override(store: DomainStore | None) -> None:
    global _store_override
    _store_override = store


def get_store() -> DomainStore:
    global _memory_singleton

    if _store_override is not None:
        return _store_override

    from app.config import get_settings
    from app.firebase_auth import firebase_status, init_firebase

    settings = get_settings()
    if settings.use_memory_store:
        if _memory_singleton is None:
            _memory_singleton = MemoryStore()
        return _memory_singleton

    init_firebase()
    status = firebase_status()
    if not status.get("initialized"):
        if settings.allow_dev_bearer:
            if _memory_singleton is None:
                _memory_singleton = MemoryStore()
            logger.warning("Using MemoryStore (Firebase not initialized)")
            return _memory_singleton
        from fastapi import HTTPException

        raise HTTPException(
            status_code=503,
            detail={
                "error": "firebase_unavailable",
                "message": "Firestore requires Firebase Admin credentials",
            },
        )

    from firebase_admin import firestore

    return FirestoreStore(firestore.client())


def get_doc(collection: str, doc_id: str) -> dict[str, Any] | None:
    return get_store().raw_get(collection, doc_id)


def put_doc(collection: str, doc_id: str, data: dict[str, Any]) -> None:
    get_store().raw_put(collection, doc_id, data)


def query_eq(collection: str, field: str, value: Any) -> list[dict[str, Any]]:
    return get_store().raw_query_eq(collection, field, value)
