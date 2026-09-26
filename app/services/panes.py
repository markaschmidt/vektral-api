"""Pane CRUD — Firestore / memory backed.

Preview panes (``kind=preview``) are VR screens with a ``preview_url``.
Chat panes (``kind=chat``) are conversation threads; messages live in agent KV
keyed by ``pane_id``.
"""

from __future__ import annotations

import re
import secrets
from typing import Any

from fastapi import HTTPException

from app.schemas import message_view, pane_view
from app.services.workspaces import require_workspace_access
from app.agent_kv import get_doc, new_id, now_iso, put_doc, query_eq

PANE_KIND_PREVIEW = "preview"
PANE_KIND_CHAT = "chat"
PANE_KIND_BLANK = "blank"
HISTORY_LIMIT = 12
HISTORY_CHARS = 1500

_KNOWN_ROUTES = {
    "home": "/",
    "homepage": "/",
    "landing": "/",
    "index": "/",
    "about": "/about",
    "checkout": "/checkout",
    "pricing": "/pricing",
    "contact": "/contact",
    "blog": "/blog",
    "catalog": "/catalog",
    "login": "/login",
    "signup": "/signup",
    "cart": "/cart",
    "search": "/search",
    "settings": "/settings",
    "profile": "/profile",
}

_SCREEN_NOUN = r"(?:screens?|panes?|windows?|displays?)"
_CREATE_SCREEN_RE = re.compile(
    rf"\b(create|add|spawn|put up|bring up|make me|give me|i need|i want|new)\b"
    rf".{{0,48}}\b{_SCREEN_NOUN}\b",
    re.I,
)
_OPEN_NAMED_RE = re.compile(
    r"\b(open|show|bring up)\b(?:\s+the)?\s+"
    r"(?:(/[\w\-./]+)|([a-z0-9][\w\-]{1,40}))"
    r"(?:\s+(?:page|screen|pane|route))?\b",
    re.I,
)
_NAMED_TARGET_RE = re.compile(
    r"\b(?:called|named|titled|for|at)\s+(?:the\s+)?(/[\w\-./]+|[a-z0-9][\w\-]{1,40})",
    re.I,
)
_PATH_RE = re.compile(r"(?<![\w])/[\w\-./]+")
_EDIT_RE = re.compile(
    r"\b(make|change|edit|fix|refactor|implement|rewrite|restyle|recolor|css|commit|button)\b",
    re.I,
)
_ISSUE_RE = re.compile(r"\b(issue|ticket|linear|pr\b|pull request)\b", re.I)
_DEICTIC_ONLY_RE = re.compile(r"\b(this|that)\s+(screen|pane|window|display)\b", re.I)
_FORCE_NEW_RE = re.compile(r"\b(another|one more|extra)\b", re.I)
_FOR_LIST_RE = re.compile(
    rf"\b{_SCREEN_NOUN}\s+for\s+(.+)$",
    re.I,
)
_BARE_SCREEN_RE = re.compile(
    rf"\b(?:open|show|create|add|spawn|place|put up|bring up|make|give me|i need|i want)\b"
    rf".{{0,48}}\b{_SCREEN_NOUN}\b",
    re.I,
)
# "place the shop page pane" / "create shop screen" — name before the noun.
_PLACE_NAMED_RE = re.compile(
    rf"\b(?:place|put|create|add|open|show|spawn|make)\b(?:\s+(?:the|a|an))?\s+"
    rf"(?:a\s+new\s+|new\s+)?([a-z0-9][\w\-]{{0,40}})\s+(?:page\s+)?{_SCREEN_NOUN}\b",
    re.I,
)
_CLOSE_PANE_RE = re.compile(
    rf"\b(?:close|remove|delete|destroy|get\s+rid\s+of|kill)\b"
    rf".{{0,48}}\b{_SCREEN_NOUN}\b",
    re.I,
)
_UPDATE_PANE_RE = re.compile(
    rf"\b(?:rename|retitle|repoint|re-point|point|move|update|change|set)\b"
    rf".{{0,64}}\b{_SCREEN_NOUN}\b",
    re.I,
)
_RENAME_TO_RE = re.compile(
    r"\brename\b.+?\bto\b\s+(?P<name>[a-z0-9][\w\- ]{0,60})",
    re.I,
)
_POINT_AT_RE = re.compile(
    r"\bpoint\b.+?\bat\b\s+(?P<route>/[\w\-./]+|[a-z0-9][\w\-]{0,40})",
    re.I,
)
_BLANK_SCREEN_RE = re.compile(
    rf"^(?:please\s+)?(?:can you\s+)?"
    rf"(?:create|add|open|show|spawn|place|put|make|give me|i need|i want)\b"
    rf".{{0,40}}\b(?:a\s+|an\s+|the\s+)?(?:new\s+)?(?:blank|empty)\b"
    rf".{{0,16}}\b{_SCREEN_NOUN}\b\s*[.!?]?$",
    re.I,
)
_CONVERT_RE = re.compile(
    r"\b(?:turn|convert|make|change)\b.{0,80}?\b(?:into|to)\b\s+"
    r"(?:a\s+|an\s+|the\s+)?(?P<dest>.+)$",
    re.I,
)
_STOP = {"the", "a", "an", "me", "it", "this", "that", "up", "my", "new", "please"}
# Filler after open/create — not a product route. "open another screen" is a new pane.
_GENERIC_TOKEN = _STOP | {
    "another",
    "extra",
    "blank",
    "fresh",
    "additional",
    "one",
    "more",
    "screen",
    "screens",
    "pane",
    "panes",
    "window",
    "windows",
    "display",
    "displays",
}


def pane_kind(doc: dict[str, Any] | None) -> str:
    raw = str((doc or {}).get("kind") or "").strip().lower()
    if raw == PANE_KIND_CHAT:
        return PANE_KIND_CHAT
    if raw == PANE_KIND_BLANK:
        return PANE_KIND_BLANK
    return PANE_KIND_PREVIEW


def is_chat_pane(doc: dict[str, Any] | None) -> bool:
    return pane_kind(doc) == PANE_KIND_CHAT


def is_blank_pane(doc: dict[str, Any] | None) -> bool:
    return pane_kind(doc) == PANE_KIND_BLANK


def preview_base_for(workspace_id: str) -> str:
    sess = get_doc("preview_sessions", workspace_id)
    if sess and sess.get("preview_base_url"):
        return str(sess["preview_base_url"])
    return ""


def _state_id(uid: str, workspace_id: str) -> str:
    return f"{uid}_{workspace_id}"


def get_active_chat_pane_id(uid: str, workspace_id: str) -> str:
    doc = get_doc("pane_state", _state_id(uid, workspace_id)) or {}
    return str(doc.get("active_chat_pane_id") or "")


def set_active_chat_pane_id(uid: str, workspace_id: str, pane_id: str) -> None:
    put_doc(
        "pane_state",
        _state_id(uid, workspace_id),
        {
            "id": _state_id(uid, workspace_id),
            "owner_uid": uid,
            "workspace_id": workspace_id,
            "active_chat_pane_id": pane_id,
            "updated_at": now_iso(),
        },
    )


def list_panes(
    workspace_id: str,
    uid: str,
    kind: str | None = None,
) -> list[dict[str, Any]]:
    require_workspace_access(workspace_id, uid)
    base = preview_base_for(workspace_id)
    want = (kind or "").strip().lower()
    docs = [
        d
        for d in query_eq("panes", "workspace_id", workspace_id)
        if not d.get("deleted")
    ]
    if want in (PANE_KIND_CHAT, PANE_KIND_PREVIEW, PANE_KIND_BLANK):
        docs = [d for d in docs if pane_kind(d) == want]
    docs.sort(key=lambda d: d.get("created_at") or "")
    return [pane_view(d, base) for d in docs]


def create_pane(
    workspace_id: str,
    uid: str,
    title: str = "",
    route: str = "/",
    layout_json: str = "",
    kind: str = "",
    source_kind: str = "",
    external_url: str = "",
) -> dict[str, Any]:
    require_workspace_access(workspace_id, uid)
    requested = str(kind).strip().lower()
    if requested == PANE_KIND_CHAT:
        k = PANE_KIND_CHAT
    elif requested == PANE_KIND_BLANK:
        k = PANE_KIND_BLANK
    else:
        k = PANE_KIND_PREVIEW
    now = now_iso()
    pid = new_id("pane_")
    src = str(source_kind or "").strip().lower()
    ext = str(external_url or "").strip()
    if k == PANE_KIND_BLANK:
        r = ""
        name = title if title else "Blank"
        src = ""
        ext = ""
    elif k == PANE_KIND_CHAT:
        r = f"/chat/{pid}"
        name = title if title else "Chat"
        src = ""
        ext = ""
    else:
        if src == "external":
            from app.services.external_urls import validate_external_url

            ext = validate_external_url(ext)
            r = route if route else "/"
            if not r.startswith("/"):
                r = f"/{r}"
            name = title if title else "External"
        else:
            src = "workspace-preview"
            ext = ""
            r = route if route else "/"
            if not r.startswith("/"):
                r = f"/{r}"
            name = title if title else f"Page {r}"
    sess = get_doc("preview_sessions", workspace_id) or {}
    runtime_id = str(sess.get("runtime_id") or "")
    status = str(sess.get("status") or "")
    if status == "running":
        status = "ready"
    doc = {
        "id": pid,
        "workspace_id": workspace_id,
        "title": name,
        "route": r,
        "kind": k,
        "source_kind": src,
        "runtime_id": runtime_id if src == "workspace-preview" else "",
        "renderer": "webview" if k == PANE_KIND_PREVIEW else "",
        "external_url": ext,
        "preview_status": "" if k != PANE_KIND_PREVIEW else ("unsupported" if src == "external" else status),
        "unsupported_reason": "external_frame_policy" if src == "external" else "",
        "reload_policy": "remount" if src == "workspace-preview" else "",
        "layout_json": layout_json or "",
        "reload_version": 0,
        "preview_session_id": secrets.token_hex(8) if k == PANE_KIND_PREVIEW and src != "external" else "",
        "created_at": now,
        "updated_at": now,
        "deleted": False,
        "owner_uid": uid,
    }
    put_doc("panes", pid, doc)
    if k == PANE_KIND_CHAT:
        set_active_chat_pane_id(uid, workspace_id, pid)
    elif src == "workspace-preview" and k == PANE_KIND_PREVIEW:
        if not sess.get("default_preview_pane_id"):
            sess = dict(sess)
            sess["id"] = workspace_id
            sess["workspace_id"] = workspace_id
            sess["default_preview_pane_id"] = pid
            sess["updated_at"] = now
            put_doc("preview_sessions", workspace_id, sess)
    return pane_view(doc, preview_base_for(workspace_id))


def find_pane(pane_id: str) -> dict[str, Any] | None:
    doc = get_doc("panes", pane_id)
    if not doc or doc.get("deleted"):
        return None
    return doc


def pane_preview_session_id(pane: dict[str, Any]) -> str:
    """Stable opaque origin id per pane so storage survives reloads."""
    sid = str(pane.get("preview_session_id") or "").strip()
    if sid:
        return sid
    pid = str(pane.get("id") or "")
    if (
        not pid
        or is_chat_pane(pane)
        or is_blank_pane(pane)
        or str(pane.get("source_kind") or "") == "external"
    ):
        return ""
    sid = secrets.token_hex(8)
    pane["preview_session_id"] = sid
    pane["updated_at"] = now_iso()
    put_doc("panes", pid, pane)
    return sid


def _release_active_chat(pane: dict[str, Any], workspace_id: str, uid: str) -> None:
    pid = str(pane.get("id") or "")
    if not pid or not is_chat_pane(pane):
        return
    if get_active_chat_pane_id(uid, workspace_id) != pid:
        return
    set_active_chat_pane_id(uid, workspace_id, "")


def _clear_surface(pane: dict[str, Any]) -> None:
    pane["source_kind"] = ""
    pane["runtime_id"] = ""
    pane["renderer"] = ""
    pane["external_url"] = ""
    pane["preview_status"] = ""
    pane["unsupported_reason"] = ""
    pane["reload_policy"] = ""
    pane["preview_session_id"] = ""
    pane["launch_url"] = ""
    pane["preview_url"] = ""


def _apply_blank(pane: dict[str, Any], title: str) -> None:
    """Keep the pane id and drop every surface. Same panel, now empty."""
    pane["kind"] = PANE_KIND_BLANK
    pane["route"] = ""
    if title:
        pane["title"] = title
    elif not str(pane.get("title") or "").strip():
        pane["title"] = "Blank"
    _clear_surface(pane)


def _apply_chat(pane: dict[str, Any], workspace_id: str, uid: str, title: str) -> None:
    """Rewrite this pane as a chat thread and inject the chat route."""
    pid = str(pane.get("id") or "")
    pane["kind"] = PANE_KIND_CHAT
    pane["route"] = f"/chat/{pid}"
    if title:
        pane["title"] = title
    elif str(pane.get("title") or "").strip() in {"", "Blank"}:
        pane["title"] = "Chat"
    _clear_surface(pane)
    pane["route"] = f"/chat/{pid}"
    set_active_chat_pane_id(uid, workspace_id, pid)


def _apply_external(pane: dict[str, Any], url: str, title: str, route: str = "") -> None:
    """Rewrite this pane as an outside site and inject the URL."""
    from app.services.external_urls import validate_external_url

    ext = validate_external_url(url)
    pane["kind"] = PANE_KIND_PREVIEW
    pane["source_kind"] = "external"
    pane["external_url"] = ext
    path = route if str(route).startswith("/") else (f"/{route}" if route else "/")
    pane["route"] = path
    pane["runtime_id"] = ""
    pane["renderer"] = "webview"
    pane["preview_status"] = "unsupported"
    pane["unsupported_reason"] = "external_frame_policy"
    pane["reload_policy"] = ""
    pane["preview_session_id"] = ""
    pane["launch_url"] = ""
    pane["preview_url"] = ""
    if title:
        pane["title"] = title
    elif str(pane.get("title") or "").strip() in {"", "Blank"}:
        pane["title"] = "External"


def _apply_workspace_preview(
    pane: dict[str, Any],
    workspace_id: str,
    route: str,
    title: str,
) -> None:
    """Rewrite this pane as a workspace page and inject the route."""
    path = _normalize_route(route) if route else str(pane.get("route") or "")
    if not path:
        path = "/"
    if not path.startswith("/"):
        path = f"/{path}"
    sess = get_doc("preview_sessions", workspace_id) or {}
    runtime_id = str(sess.get("runtime_id") or "")
    status = str(sess.get("status") or "")
    if status == "running":
        status = "ready"
    pane["kind"] = PANE_KIND_PREVIEW
    pane["source_kind"] = "workspace-preview"
    pane["route"] = path
    pane["external_url"] = ""
    pane["unsupported_reason"] = ""
    pane["runtime_id"] = runtime_id
    pane["renderer"] = "webview"
    pane["reload_policy"] = "remount"
    pane["preview_status"] = status
    pane["launch_url"] = ""
    pane["preview_url"] = ""
    if not str(pane.get("preview_session_id") or "").strip():
        pane["preview_session_id"] = secrets.token_hex(8)
    if title:
        pane["title"] = title
    elif str(pane.get("title") or "").strip() in {"", "Blank"}:
        pane["title"] = _title_from_route(path)
    pane["reload_version"] = int(pane.get("reload_version") or 0) + 1


def update_pane(
    workspace_id: str,
    pane_id: str,
    uid: str,
    title: str = "",
    route: str = "",
    layout_json: str = "",
    kind: str = "",
    source_kind: str = "",
    external_url: str = "",
) -> dict[str, Any] | None:
    pane = find_pane(pane_id)
    if pane is None or pane.get("workspace_id") != workspace_id:
        return None
    require_workspace_access(workspace_id, uid)
    requested = str(kind or "").strip().lower()
    src_in = str(source_kind or "").strip().lower()
    ext_in = str(external_url or "").strip()
    current = pane_kind(pane)

    def _finish() -> dict[str, Any]:
        if layout_json:
            pane["layout_json"] = layout_json
        pane["updated_at"] = now_iso()
        put_doc("panes", pane_id, pane)
        return pane_view(pane, preview_base_for(workspace_id))

    # Same pane id. The new type replaces the old surface and receives its fields.
    if requested == PANE_KIND_BLANK and current != PANE_KIND_BLANK:
        _release_active_chat(pane, workspace_id, uid)
        _apply_blank(pane, title)
        return _finish()
    if requested == PANE_KIND_CHAT and current != PANE_KIND_CHAT:
        _apply_chat(pane, workspace_id, uid, title)
        return _finish()
    blank_fill = current == PANE_KIND_BLANK and (
        bool(route) or src_in == "external" or bool(ext_in) or requested == PANE_KIND_PREVIEW
    )
    type_to_preview = requested == PANE_KIND_PREVIEW and current != PANE_KIND_PREVIEW
    if blank_fill or type_to_preview or (
        current != PANE_KIND_PREVIEW and (src_in == "external" or bool(ext_in))
    ):
        _release_active_chat(pane, workspace_id, uid)
        if src_in == "external" or ext_in:
            _apply_external(pane, ext_in or str(pane.get("external_url") or ""), title, route)
        else:
            _apply_workspace_preview(pane, workspace_id, route, title)
        return _finish()

    if title:
        pane["title"] = title
    if requested == PANE_KIND_CHAT:
        pane["kind"] = PANE_KIND_CHAT
    elif requested == PANE_KIND_PREVIEW and not is_blank_pane(pane):
        pane["kind"] = PANE_KIND_PREVIEW
    if route and not is_chat_pane(pane) and not is_blank_pane(pane):
        r = route if route.startswith("/") else f"/{route}"
        pane["route"] = r
    if layout_json:
        pane["layout_json"] = layout_json
    src = str(source_kind or "").strip().lower()
    if src == "external" and not is_chat_pane(pane):
        from app.services.external_urls import validate_external_url

        pane["source_kind"] = "external"
        pane["external_url"] = validate_external_url(external_url or pane.get("external_url") or "")
        pane["runtime_id"] = ""
        pane["unsupported_reason"] = "external_frame_policy"
        pane["preview_status"] = "unsupported"
    elif src == "workspace-preview" and not is_chat_pane(pane):
        pane["source_kind"] = "workspace-preview"
        pane["external_url"] = ""
        pane["unsupported_reason"] = ""
    elif external_url and not is_chat_pane(pane):
        from app.services.external_urls import validate_external_url

        pane["source_kind"] = "external"
        pane["external_url"] = validate_external_url(external_url)
        pane["runtime_id"] = ""
        pane["unsupported_reason"] = "external_frame_policy"
        pane["preview_status"] = "unsupported"
    pane["updated_at"] = now_iso()
    put_doc("panes", pane_id, pane)
    return pane_view(pane, preview_base_for(workspace_id))


def delete_pane(workspace_id: str, pane_id: str, uid: str) -> dict[str, Any]:
    pane = find_pane(pane_id)
    if pane is None or pane.get("workspace_id") != workspace_id:
        return {"ok": False, "message": "pane not found", "id": pane_id}
    require_workspace_access(workspace_id, uid)
    pane["deleted"] = True
    pane["title"] = "[deleted]"
    pane["updated_at"] = now_iso()
    put_doc("panes", pane_id, pane)
    if get_active_chat_pane_id(uid, workspace_id) == pane_id:
        remaining = [
            d
            for d in query_eq("panes", "workspace_id", workspace_id)
            if not d.get("deleted") and is_chat_pane(d)
        ]
        remaining.sort(key=lambda d: d.get("created_at") or "")
        next_id = remaining[0]["id"] if remaining else ""
        set_active_chat_pane_id(uid, workspace_id, next_id)
    return {"ok": True, "message": "pane deleted", "id": pane_id}


def bump_pane_reload(pane_id: str) -> dict[str, Any] | None:
    pane = find_pane(pane_id)
    if pane is None:
        return None
    if is_chat_pane(pane):
        return pane_view(pane, preview_base_for(str(pane.get("workspace_id") or "")))
    pane["reload_version"] = int(pane.get("reload_version") or 0) + 1
    pane["launch_url"] = ""
    pane["preview_url"] = ""
    pane["updated_at"] = now_iso()
    put_doc("panes", pane_id, pane)
    return pane_view(pane, preview_base_for(str(pane.get("workspace_id") or "")))


def open_structured_pane(
    workspace_id: str,
    uid: str,
    *,
    title: str = "",
    route: str = "",
    force_new: bool = False,
    kind: str = "",
) -> dict[str, Any]:
    """Create or reuse a preview pane from a validated structured action."""
    require_workspace_access(workspace_id, uid)
    if str(kind).strip().lower() == PANE_KIND_BLANK:
        name = (title or "").strip() or "Blank"
        created = create_pane(workspace_id, uid, title=name, kind=PANE_KIND_BLANK)
        created["created"] = True
        return created
    name = (title or "").strip()
    generic = not name or name.lower() in _GENERIC_TOKEN
    # An unnamed "another screen" gets its own route (/screen-2) and title, so
    # later "this screen" / "the screen 2" resolution is never ambiguous.
    path = _normalize_route(route) if route else ("" if force_new and generic else _normalize_route(name))
    if not path:
        path = _unique_screen_route(workspace_id)
    if not name or name.startswith("/") or (force_new and generic):
        name = _title_from_route(path)
    existing = _preview_docs(workspace_id)
    if not force_new:
        for pane in existing:
            if str(pane.get("route") or "") == path:
                return {**pane_view(pane, preview_base_for(workspace_id)), "created": False}
            if str(pane.get("title") or "").strip().lower() == name.strip().lower():
                return {**pane_view(pane, preview_base_for(workspace_id)), "created": False}
    created = create_pane(workspace_id, uid, title=name, route=path)
    created["created"] = True
    return created


def set_preview_route(
    workspace_id: str,
    uid: str,
    pane_id: str,
    route: str,
) -> dict[str, Any]:
    """Rebind an existing workspace-preview pane to a canonical route and remount."""
    require_workspace_access(workspace_id, uid)
    pane = find_pane(pane_id)
    if pane is None or pane.get("workspace_id") != workspace_id or pane.get("deleted"):
        raise HTTPException(
            status_code=404,
            detail={"error": "not_found", "message": "pane not found"},
        )
    if is_chat_pane(pane):
        raise HTTPException(
            status_code=400,
            detail={"error": "invalid_pane", "message": "chat panes cannot set a preview route"},
        )
    if is_blank_pane(pane):
        path = _normalize_route(route) or "/"
        _apply_workspace_preview(pane, workspace_id, path, "")
        pane["updated_at"] = now_iso()
        put_doc("panes", pane_id, pane)
        return pane_view(pane, preview_base_for(workspace_id))
    if str(pane.get("source_kind") or "workspace-preview") == "external":
        raise HTTPException(
            status_code=400,
            detail={"error": "invalid_pane", "message": "external panes cannot set a preview route"},
        )
    path = _normalize_route(route) or "/"
    if not path.startswith("/"):
        path = f"/{path}"
    pane["route"] = path
    if not str(pane.get("title") or "").strip():
        pane["title"] = _title_from_route(path)
    pane["reload_version"] = int(pane.get("reload_version") or 0) + 1
    pane["launch_url"] = ""
    pane["preview_url"] = ""
    pane["updated_at"] = now_iso()
    put_doc("panes", pane_id, pane)
    return pane_view(pane, preview_base_for(workspace_id))


def bump_workspace_panes(workspace_id: str) -> list[dict[str, Any]]:
    base = preview_base_for(workspace_id)
    out: list[dict[str, Any]] = []
    for pane in query_eq("panes", "workspace_id", workspace_id):
        if pane.get("deleted") or is_chat_pane(pane) or is_blank_pane(pane):
            continue
        pane["reload_version"] = int(pane.get("reload_version") or 0) + 1
        pane["launch_url"] = ""
        pane["preview_url"] = ""
        pane["updated_at"] = now_iso()
        put_doc("panes", pane["id"], pane)
        out.append(pane_view(pane, base))
    return out


def ensure_chat_pane(workspace_id: str, uid: str) -> dict[str, Any]:
    require_workspace_access(workspace_id, uid)
    chats = [
        d
        for d in query_eq("panes", "workspace_id", workspace_id)
        if not d.get("deleted") and is_chat_pane(d)
    ]
    if chats:
        chats.sort(key=lambda d: d.get("created_at") or "")
        active = get_active_chat_pane_id(uid, workspace_id)
        if not any(d["id"] == active for d in chats):
            set_active_chat_pane_id(uid, workspace_id, chats[0]["id"])
        return pane_view(chats[0], preview_base_for(workspace_id))
    return create_pane(workspace_id, uid, title="Chat", kind=PANE_KIND_CHAT)


def activate_chat_pane(workspace_id: str, pane_id: str, uid: str) -> dict[str, Any]:
    require_workspace_access(workspace_id, uid)
    pane = find_pane(pane_id)
    if pane is None or pane.get("workspace_id") != workspace_id:
        raise HTTPException(
            status_code=404, detail={"error": "not_found", "message": "pane not found"}
        )
    if not is_chat_pane(pane):
        raise HTTPException(
            status_code=400,
            detail={"error": "invalid_kind", "message": "only chat panes can be activated"},
        )
    set_active_chat_pane_id(uid, workspace_id, pane_id)
    return pane_view(pane, preview_base_for(workspace_id))


def _home_preview_route(workspace_id: str) -> str:
    docs = [
        d
        for d in query_eq("panes", "workspace_id", workspace_id)
        if not d.get("deleted") and not is_chat_pane(d)
    ]
    for d in docs:
        if (d.get("route") or "/") == "/":
            return "/"
    if docs:
        r = str(docs[0].get("route") or "/")
        return r if r.startswith("/") else f"/{r}"
    return "/"


def preview_route_for_coding(
    workspace_id: str,
    route: str = "",
    pane_id: str = "",
) -> str:
    pane = find_pane(pane_id) if pane_id else None
    r = route or (pane.get("route") if pane else "") or "/"
    if not str(r).startswith("/"):
        r = f"/{r}"
    if str(r).startswith("/chat") or is_chat_pane(pane):
        return _home_preview_route(workspace_id)
    return str(r)


def resolve_chat_pane(
    uid: str,
    workspace_id: str,
    pane_id: str = "",
    new_conversation: bool = False,
) -> dict[str, Any]:
    """Pick the chat pane for history and the preview route for coding."""
    require_workspace_access(workspace_id, uid)
    preview_route = _home_preview_route(workspace_id)
    preview_pane_id = ""

    if new_conversation:
        created = create_pane(workspace_id, uid, title="New chat", kind=PANE_KIND_CHAT)
        return {
            "chat_pane_id": created["id"],
            "preview_route": preview_route,
            "preview_pane_id": "",
        }

    given = find_pane(pane_id) if pane_id else None
    if given and given.get("workspace_id") == workspace_id:
        if is_chat_pane(given):
            set_active_chat_pane_id(uid, workspace_id, given["id"])
            return {
                "chat_pane_id": given["id"],
                "preview_route": preview_route,
                "preview_pane_id": "",
            }
        preview_route = str(given.get("route") or "/") or "/"
        if not preview_route.startswith("/"):
            preview_route = f"/{preview_route}"
        preview_pane_id = given["id"]

    active_id = get_active_chat_pane_id(uid, workspace_id)
    active = find_pane(active_id) if active_id else None
    if active and is_chat_pane(active) and active.get("workspace_id") == workspace_id:
        return {
            "chat_pane_id": active["id"],
            "preview_route": preview_route,
            "preview_pane_id": preview_pane_id,
        }

    created = ensure_chat_pane(workspace_id, uid)
    return {
        "chat_pane_id": created["id"],
        "preview_route": preview_route,
        "preview_pane_id": preview_pane_id,
    }


def append_message(
    pane_id: str,
    uid: str,
    role: str,
    text: str,
    job_id: str = "",
) -> dict[str, Any]:
    now = now_iso()
    mid = new_id("msg_")
    doc = {
        "id": mid,
        "pane_id": pane_id,
        "owner_uid": uid,
        "role": role if role in ("user", "assistant") else "user",
        "text": text or "",
        "job_id": job_id,
        "created_at": now,
    }
    put_doc("messages", mid, doc)
    pane = find_pane(pane_id)
    if pane:
        pane["updated_at"] = now
        if role == "user" and not (pane.get("title") or "").strip():
            pane["title"] = (text or "Chat").strip()[:80] or "Chat"
        elif role == "user" and pane.get("title") in ("Chat", "New chat"):
            snippet = (text or "").strip().split("\n")[0][:80]
            if snippet:
                pane["title"] = snippet
        put_doc("panes", pane_id, pane)
    return message_view(doc)


def list_messages(workspace_id: str, pane_id: str, uid: str) -> list[dict[str, Any]]:
    require_workspace_access(workspace_id, uid)
    pane = find_pane(pane_id)
    if pane is None or pane.get("workspace_id") != workspace_id:
        raise HTTPException(
            status_code=404, detail={"error": "not_found", "message": "pane not found"}
        )
    if not is_chat_pane(pane):
        raise HTTPException(
            status_code=404,
            detail={"error": "not_found", "message": "messages are only on chat panes"},
        )
    docs = query_eq("messages", "pane_id", pane_id)
    docs.sort(key=lambda d: d.get("created_at") or "")
    return [message_view(d) for d in docs]


def history_for_llm(
    pane_id: str,
    limit: int = HISTORY_LIMIT,
    exclude_text: str = "",
) -> list[dict[str, str]]:
    if not pane_id:
        return []
    docs = query_eq("messages", "pane_id", pane_id)
    docs.sort(key=lambda d: d.get("created_at") or "")
    recent = docs[-limit:] if limit > 0 else docs
    out: list[dict[str, str]] = []
    skip = (exclude_text or "").strip()[:HISTORY_CHARS]
    for d in recent:
        role = str(d.get("role") or "user")
        if role not in ("user", "assistant"):
            role = "user"
        text = str(d.get("text") or "")[:HISTORY_CHARS]
        if text:
            out.append({"role": role, "content": text})
    if skip and out and out[-1].get("role") == "user" and out[-1].get("content") == skip:
        out = out[:-1]
    return out


def attach_runtime_to_preview_panes(workspace_id: str, runtime: dict[str, Any]) -> None:
    rid = str(runtime.get("runtimeId") or runtime.get("runtime_id") or "")
    status = str(runtime.get("status") or "")
    policy = str(runtime.get("reloadPolicy") or runtime.get("reload_policy") or "remount")
    for pane in query_eq("panes", "workspace_id", workspace_id):
        if pane.get("deleted") or is_chat_pane(pane) or is_blank_pane(pane):
            continue
        if str(pane.get("source_kind") or "workspace-preview") == "external":
            continue
        pane["source_kind"] = "workspace-preview"
        pane["runtime_id"] = rid
        pane["renderer"] = pane.get("renderer") or "webview"
        pane["reload_policy"] = policy
        pane["preview_status"] = status
        pane["updated_at"] = now_iso()
        put_doc("panes", pane["id"], pane)


def runtime_pane_ids(workspace_id: str, runtime_id: str) -> list[str]:
    out: list[str] = []
    for pane in query_eq("panes", "workspace_id", workspace_id):
        if pane.get("deleted") or is_chat_pane(pane) or is_blank_pane(pane):
            continue
        if str(pane.get("source_kind") or "workspace-preview") == "external":
            continue
        if runtime_id and str(pane.get("runtime_id") or "") not in {"", runtime_id}:
            continue
        out.append(str(pane["id"]))
    return out


def compact_pane_item(view: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": view.get("id") or "",
        "title": view.get("title") or "",
        "kind": view.get("kind") or PANE_KIND_PREVIEW,
        "route": view.get("route") or "/",
        "updated_at": view.get("updated_at") or "",
    }


def _title_from_route(route: str) -> str:
    raw = (route or "/").strip("/")
    if not raw:
        return "Home"
    return " ".join(part.replace("-", " ").replace("_", " ").title() for part in raw.split("/") if part)


def _normalize_route(raw: str) -> str:
    token = (raw or "").strip().strip(".,!?").lower()
    if not token:
        return ""
    if token in _KNOWN_ROUTES:
        return _KNOWN_ROUTES[token]
    if token.startswith("/"):
        path = "/" + token.strip("/").strip()
        return path if path != "/" else "/"
    slug = re.sub(r"[^a-z0-9]+", "-", token).strip("-")
    if not slug:
        return ""
    return _KNOWN_ROUTES.get(slug, f"/{slug}")


def _preview_docs(workspace_id: str) -> list[dict[str, Any]]:
    return [
        d
        for d in query_eq("panes", "workspace_id", workspace_id)
        if not d.get("deleted") and not is_chat_pane(d)
    ]


def _unique_screen_route(workspace_id: str) -> str:
    used = {str(p.get("route") or "") for p in _preview_docs(workspace_id)}
    if "/screen" not in used:
        return "/screen"
    n = 2
    while f"/screen-{n}" in used:
        n += 1
    return f"/screen-{n}"


def _split_names(blob: str) -> list[str]:
    parts = re.split(r"\s*(?:,|\band\b|&)\s*", blob.strip(), flags=re.I)
    out: list[str] = []
    for part in parts:
        name = part.strip().strip(".,!?")
        if not name or name.lower() in _STOP:
            continue
        out.append(name)
    return out[:8]


def _named_screen_token(raw: str) -> str:
    """Product title or route. Empty when the utterance is only 'another/new screen'."""
    placed = _PLACE_NAMED_RE.search(raw or "")
    if placed:
        token = (placed.group(1) or "").strip("/").lower()
        if token and token not in _GENERIC_TOKEN:
            return token
    if not (
        _CREATE_SCREEN_RE.search(raw)
        or _BARE_SCREEN_RE.search(raw)
        or _OPEN_NAMED_RE.search(raw)
    ):
        return ""
    match_for = _FOR_LIST_RE.search(raw)
    if match_for:
        for name in _split_names(match_for.group(1)):
            token = name.strip("/").lower()
            if token and token not in _GENERIC_TOKEN:
                return token
    named = _NAMED_TARGET_RE.search(raw)
    if named:
        token = (named.group(1) or "").strip("/").lower()
        if token and token not in _GENERIC_TOKEN:
            return token
    for path in _PATH_RE.findall(raw):
        token = path.strip("/").lower()
        if token and token not in _GENERIC_TOKEN:
            return token
    opened = _OPEN_NAMED_RE.search(raw)
    if opened:
        token = (opened.group(2) or opened.group(3) or "").strip("/").lower()
        if token and token not in _GENERIC_TOKEN:
            return token
    return ""


def _unnamed_screen_request(raw: str) -> bool:
    """True for 'open another screen' / 'place a new screen' with no product name."""
    if _named_screen_token(raw):
        return False
    if (
        _DEICTIC_ONLY_RE.search(raw)
        and not _FORCE_NEW_RE.search(raw)
        and not _CREATE_SCREEN_RE.search(raw)
    ):
        return False
    if _CREATE_SCREEN_RE.search(raw) or _BARE_SCREEN_RE.search(raw):
        return True
    return bool(_FORCE_NEW_RE.search(raw) and re.search(_SCREEN_NOUN, raw, re.I))


def is_pane_close_command(text: str) -> bool:
    """True for 'close/remove/delete this pane' or a named pane close."""
    raw = (text or "").strip()
    if not raw or not _CLOSE_PANE_RE.search(raw):
        return False
    if _ISSUE_RE.search(raw):
        return False
    return True


def is_blank_screen_request(text: str) -> bool:
    """True for 'place a blank screen' — an empty pane, not a page route."""
    raw = (text or "").strip()
    if not raw or _ISSUE_RE.search(raw):
        return False
    return bool(_BLANK_SCREEN_RE.search(raw))


def _conversion_fields(raw: str) -> dict[str, str]:
    """Type change spoken as 'turn this screen into …'. Empty when it is not one."""
    match = _CONVERT_RE.search(raw or "")
    if not match or not re.search(_SCREEN_NOUN, raw or "", re.I):
        return {}
    dest = (match.group("dest") or "").strip().strip(".,!?")
    dest = re.sub(rf"\s+(?:{_SCREEN_NOUN}|pages?)$", "", dest, flags=re.I).strip()
    low = dest.lower()
    if low in {"chat", "chats"} or low.startswith("chat "):
        return {"kind": PANE_KIND_CHAT}
    if low in {"blank", "empty"} or low.startswith("blank") or low.startswith("empty"):
        return {"kind": PANE_KIND_BLANK}
    url = re.match(r"https?://\S+", dest, re.I)
    if url:
        return {
            "kind": PANE_KIND_PREVIEW,
            "source_kind": "external",
            "external_url": url.group(0).rstrip(".,)"),
        }
    token = dest.strip("/").lower()
    if not token or token in _GENERIC_TOKEN:
        return {}
    route = _normalize_route(dest)
    if not route:
        return {}
    return {
        "kind": PANE_KIND_PREVIEW,
        "source_kind": "workspace-preview",
        "route": route,
        "title": _title_from_route(route),
    }


def is_pane_update_command(text: str) -> bool:
    """True for 'rename X to Y' / 'point this screen at /route' / 'turn this into …'."""
    raw = (text or "").strip()
    if not raw or _ISSUE_RE.search(raw):
        return False
    if _conversion_fields(raw):
        return True
    if not _UPDATE_PANE_RE.search(raw):
        return False
    return bool(
        _RENAME_TO_RE.search(raw)
        or _POINT_AT_RE.search(raw)
        or _PATH_RE.search(raw)
        or _named_screen_token(raw)
        or _PLACE_NAMED_RE.search(raw)
    )


def _match_pane_by_hint(
    panes: list[dict[str, Any]],
    hint: str,
) -> dict[str, Any] | None:
    token = (hint or "").strip().strip("/").lower()
    if not token or token in _GENERIC_TOKEN:
        return None
    for pane in panes:
        if str(pane.get("route") or "").strip().lower().strip("/") == token:
            return pane
    for pane in panes:
        if str(pane.get("title") or "").strip().lower() == token:
            return pane
    for pane in panes:
        title = str(pane.get("title") or "").strip().lower()
        if token and token in title:
            return pane
    return None


def resolve_pane_target(
    workspace_id: str,
    text: str,
    *,
    focused: list[str] | None = None,
    primary: str = "",
    panes: list[dict[str, Any]] | None = None,
) -> dict[str, Any] | None:
    """Resolve which preview pane a close/update utterance means.

    Order: explicit path, named token, focused/primary, single-pane fallback.
    """
    raw = (text or "").strip()
    catalog = list(panes or []) or [
        pane_view(p, preview_base_for(workspace_id)) for p in _preview_docs(workspace_id)
    ]
    previews = [p for p in catalog if str(p.get("kind") or "preview") != "chat"]
    if not previews:
        return None
    for path in _PATH_RE.findall(raw):
        hit = _match_pane_by_hint(previews, path)
        if hit:
            return hit
    for getter in (
        lambda: _named_screen_token(raw),
        lambda: (_PLACE_NAMED_RE.search(raw).group(1) if _PLACE_NAMED_RE.search(raw) else ""),
    ):
        hit = _match_pane_by_hint(previews, getter())
        if hit:
            return hit
    by_id = {str(p.get("id") or ""): p for p in previews}
    if primary and primary in by_id:
        return by_id[primary]
    for pid in focused or []:
        if pid in by_id:
            return by_id[pid]
    if len(previews) == 1:
        return previews[0]
    return None


def parse_pane_update(text: str) -> dict[str, str]:
    """Extract the new title, route, and type from an update utterance."""
    raw = (text or "").strip()
    converted = _conversion_fields(raw)
    title = str(converted.get("title") or "")
    route = str(converted.get("route") or "")
    rename = _RENAME_TO_RE.search(raw)
    if rename:
        title = (rename.group("name") or "").strip().strip(".,!?")
        if title.lower() in _GENERIC_TOKEN:
            title = ""
    point = _POINT_AT_RE.search(raw)
    if point:
        route = _normalize_route(point.group("route") or "")
    if not route:
        for path in _PATH_RE.findall(raw):
            norm = _normalize_route(path)
            if norm:
                route = norm
                break
    if not title and not route:
        token = _named_screen_token(raw)
        if token:
            route = _normalize_route(token)
    if route and not title:
        title = _title_from_route(route)
    if route and not converted.get("kind"):
        converted = {**converted, "kind": PANE_KIND_PREVIEW, "source_kind": "workspace-preview"}
    return {
        "title": title,
        "route": route,
        "kind": str(converted.get("kind") or ""),
        "source_kind": str(converted.get("source_kind") or ""),
        "external_url": str(converted.get("external_url") or ""),
    }


def is_screen_command(text: str) -> bool:
    """True when speech asks to open or spawn a preview screen, not a product edit."""
    raw = (text or "").strip()
    if is_blank_screen_request(raw):
        return True
    if not raw or _EDIT_RE.search(raw) or _ISSUE_RE.search(raw):
        return False
    if (
        _DEICTIC_ONLY_RE.search(raw)
        and not _named_screen_token(raw)
        and not _PATH_RE.search(raw)
        and not _FORCE_NEW_RE.search(raw)
    ):
        return False
    if _unnamed_screen_request(raw) or _named_screen_token(raw):
        return True
    if _CREATE_SCREEN_RE.search(raw):
        return True
    opened = _OPEN_NAMED_RE.search(raw)
    if not opened:
        return False
    token = (opened.group(2) or opened.group(3) or "").strip("/").lower()
    return bool(token) and token not in _GENERIC_TOKEN


def parse_screen_targets(text: str) -> list[dict[str, str]]:
    """Extract title/route pairs from a create/open-screen utterance."""
    raw = (text or "").strip()
    if is_blank_screen_request(raw):
        return [{"title": "Blank", "route": "", "kind": "blank", "force_new": "1"}]
    if _unnamed_screen_request(raw):
        # Unique route, and do not reuse a pane titled "Screen".
        return [{"title": "Screen", "route": "", "force_new": "1"}]
    force_new = bool(_FORCE_NEW_RE.search(raw))
    listed: list[str] = []
    match_for = _FOR_LIST_RE.search(raw)
    if match_for:
        listed = _split_names(match_for.group(1))
    targets: list[dict[str, str]] = []
    seen: set[str] = set()

    def add(title: str, route: str) -> None:
        path = _normalize_route(route or title)
        if not path:
            return
        key = path.lower()
        if key in seen:
            return
        seen.add(key)
        name = (title or "").strip()
        if not name or name.startswith("/"):
            name = _title_from_route(path)
        targets.append({"title": name, "route": path})

    if listed:
        for name in listed:
            add(name, name)
        return targets

    for path in _PATH_RE.findall(raw):
        add("", path)
    named = _NAMED_TARGET_RE.search(raw)
    if named:
        add(named.group(1), named.group(1))
    placed = _PLACE_NAMED_RE.search(raw)
    if placed and (placed.group(1) or "").strip("/").lower() not in _GENERIC_TOKEN:
        add(placed.group(1), placed.group(1))
    opened = _OPEN_NAMED_RE.search(raw)
    if opened:
        add(opened.group(2) or opened.group(3) or "", opened.group(2) or opened.group(3) or "")
    if targets:
        if force_new:
            for item in targets:
                item["force_new"] = "1"
        return targets
    if is_screen_command(raw):
        return [{"title": "Screen", "route": "", "force_new": "1" if force_new else ""}]
    return []


def open_or_create_from_utterance(
    workspace_id: str,
    uid: str,
    text: str,
) -> list[dict[str, Any]]:
    """Focus existing preview panes or create them. Used by voice/text jobs."""
    require_workspace_access(workspace_id, uid)
    specs = parse_screen_targets(text)
    if not specs:
        specs = [{"title": "Screen", "route": ""}]
    results: list[dict[str, Any]] = []
    for spec in specs:
        results.append(
            open_structured_pane(
                workspace_id,
                uid,
                title=str(spec.get("title") or ""),
                route=str(spec.get("route") or ""),
                force_new=str(spec.get("force_new") or "") == "1",
                kind=str(spec.get("kind") or ""),
            )
        )
    return results


def session_pane_slice(uid: str, workspaces: list[dict[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for ws in workspaces:
        wid = str(ws.get("id") or "")
        if not wid:
            continue
        try:
            items = list_panes(wid, uid)
        except Exception:  # noqa: BLE001
            continue
        out[wid] = {
            "active_chat_pane_id": get_active_chat_pane_id(uid, wid),
            "items": [compact_pane_item(p) for p in items],
        }
    return out
