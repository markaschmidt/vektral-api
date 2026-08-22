"""Pane CRUD — Firestore / memory backed.

Preview panes (``kind=preview``) are VR screens with a ``preview_url``.
Chat panes (``kind=chat``) are conversation threads; messages live in agent KV
keyed by ``pane_id``.
"""

from __future__ import annotations

from typing import Any

from fastapi import HTTPException

from app.schemas import message_view, pane_view
from app.services.workspaces import require_workspace_access
from app.agent_kv import get_doc, new_id, now_iso, put_doc, query_eq

PANE_KIND_PREVIEW = "preview"
PANE_KIND_CHAT = "chat"
HISTORY_LIMIT = 12
HISTORY_CHARS = 1500


def pane_kind(doc: dict[str, Any] | None) -> str:
    raw = str((doc or {}).get("kind") or "").strip().lower()
    if raw == PANE_KIND_CHAT:
        return PANE_KIND_CHAT
    return PANE_KIND_PREVIEW


def is_chat_pane(doc: dict[str, Any] | None) -> bool:
    return pane_kind(doc) == PANE_KIND_CHAT


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
    if want in (PANE_KIND_CHAT, PANE_KIND_PREVIEW):
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
) -> dict[str, Any]:
    require_workspace_access(workspace_id, uid)
    k = PANE_KIND_CHAT if str(kind).strip().lower() == PANE_KIND_CHAT else PANE_KIND_PREVIEW
    now = now_iso()
    pid = new_id("pane_")
    if k == PANE_KIND_CHAT:
        r = f"/chat/{pid}"
        name = title if title else "Chat"
    else:
        r = route if route else "/"
        if not r.startswith("/"):
            r = f"/{r}"
        name = title if title else f"Page {r}"
    doc = {
        "id": pid,
        "workspace_id": workspace_id,
        "title": name,
        "route": r,
        "kind": k,
        "layout_json": layout_json or "",
        "reload_version": 0,
        "created_at": now,
        "updated_at": now,
        "deleted": False,
        "owner_uid": uid,
    }
    put_doc("panes", pid, doc)
    if k == PANE_KIND_CHAT:
        set_active_chat_pane_id(uid, workspace_id, pid)
    return pane_view(doc, preview_base_for(workspace_id))


def find_pane(pane_id: str) -> dict[str, Any] | None:
    doc = get_doc("panes", pane_id)
    if not doc or doc.get("deleted"):
        return None
    return doc


def update_pane(
    workspace_id: str,
    pane_id: str,
    uid: str,
    title: str = "",
    route: str = "",
    layout_json: str = "",
    kind: str = "",
) -> dict[str, Any] | None:
    pane = find_pane(pane_id)
    if pane is None or pane.get("workspace_id") != workspace_id:
        return None
    require_workspace_access(workspace_id, uid)
    if title:
        pane["title"] = title
    if kind.strip():
        pane["kind"] = (
            PANE_KIND_CHAT if kind.strip().lower() == PANE_KIND_CHAT else PANE_KIND_PREVIEW
        )
    if route and not is_chat_pane(pane):
        r = route if route.startswith("/") else f"/{route}"
        pane["route"] = r
    if layout_json:
        pane["layout_json"] = layout_json
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
    pane["updated_at"] = now_iso()
    put_doc("panes", pane_id, pane)
    return pane_view(pane, preview_base_for(str(pane.get("workspace_id") or "")))


def bump_workspace_panes(workspace_id: str) -> list[dict[str, Any]]:
    base = preview_base_for(workspace_id)
    out: list[dict[str, Any]] = []
    for pane in query_eq("panes", "workspace_id", workspace_id):
        if pane.get("deleted") or is_chat_pane(pane):
            continue
        pane["reload_version"] = int(pane.get("reload_version") or 0) + 1
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


def compact_pane_item(view: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": view.get("id") or "",
        "title": view.get("title") or "",
        "kind": view.get("kind") or PANE_KIND_PREVIEW,
        "route": view.get("route") or "/",
        "updated_at": view.get("updated_at") or "",
    }


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
