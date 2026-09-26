"""Voice-to-action catalog.

Pane placement is the reference action: a user utterance becomes
``pane.open``, which executes inline and returns ``result_json.action``
``open_screen``. Later voice actions follow the same shape — a
``TurnActionV1`` variant, a stage in ``CHEAP_ACTION_TYPES`` or
``QUEUED_ACTION_TYPES``, and a typed result the client can paint.
"""

from __future__ import annotations

from typing import Any

MIN_TURN_ID_LEN = 8
MAX_TURN_ID_LEN = 80

ACTION_PANE_OPEN = "pane.open"
ACTION_PANE_UPDATE = "pane.update"
ACTION_PANE_CLOSE = "pane.close"
RESULT_OPEN_SCREEN = "open_screen"
RESULT_UPDATE_PANE = "update_pane"
RESULT_CLOSE_PANE = "close_pane"

PANE_CRUD_TYPES = frozenset({"pane.open", "pane.set_route", "pane.update", "pane.close"})

CHEAP_ACTION_TYPES = frozenset({"noop", "pane.open", "pane.set_route", "pane.update", "pane.close", "avatar.plan"})
QUEUED_ACTION_TYPES = frozenset({"page.edit", "page.create", "research.run", "linear.run"})


def normalize_turn_id(raw: str, *, required: bool) -> tuple[str, dict[str, str] | None]:
    """Return (turn_id, error_detail). Empty is allowed only when not required."""
    turn_id = (raw or "").strip()
    if not turn_id:
        if required:
            return "", {
                "error": "missing_turn_id",
                "message": "turn_id is required for a finalized vocalbridge query",
            }
        return "", None
    if len(turn_id) < MIN_TURN_ID_LEN or len(turn_id) > MAX_TURN_ID_LEN:
        return "", {
            "error": "invalid_turn_id",
            "message": f"turn_id must be {MIN_TURN_ID_LEN}-{MAX_TURN_ID_LEN} characters",
        }
    return turn_id, None


def open_screen_result(panes: list[dict[str, Any]], summary: str = "") -> dict[str, Any]:
    """Typed payload the VR client paints. Speech never carries this shape."""
    items = []
    for p in panes:
        item: dict[str, Any] = {
            "id": p.get("id"),
            "title": p.get("title"),
            "route": p.get("route"),
            "created": bool(p.get("created")),
        }
        kind = str(p.get("kind") or "")
        if kind:
            item["kind"] = kind
        items.append(item)
    text = (summary or "").strip()
    if not text:
        created_n = sum(1 for item in items if item["created"])
        names = ", ".join(
            str(item.get("title") or item.get("route") or "screen") for item in items
        ) or "screen"
        if created_n:
            text = f"Created {names}." if created_n > 1 else f"Created the {names} screen."
        else:
            text = f"Opened the {names} screen."
    return {
        "ok": True,
        "action": RESULT_OPEN_SCREEN,
        "summary": text,
        "panes": items,
    }


def close_pane_result(pane_id: str, title: str = "", summary: str = "") -> dict[str, Any]:
    """Typed payload when a pane is removed. Client destroys the panel."""
    text = (summary or "").strip() or f"Removed {title or pane_id or 'the pane'}."
    return {
        "ok": True,
        "action": RESULT_CLOSE_PANE,
        "summary": text,
        "panes": [{"id": pane_id, "title": title, "route": "", "created": False, "kind": ""}],
    }


def update_pane_result(pane: dict[str, Any], summary: str = "") -> dict[str, Any]:
    """Typed payload when a pane is renamed or repointed."""
    text = (summary or "").strip() or (
        f"Updated {pane.get('title') or pane.get('route') or 'the pane'}."
    )
    return {
        "ok": True,
        "action": RESULT_UPDATE_PANE,
        "summary": text,
        "panes": [
            {
                "id": pane.get("id"),
                "title": pane.get("title"),
                "route": pane.get("route"),
                "created": False,
                "kind": pane.get("kind") or "",
            }
        ],
    }


def merge_planned_actions(
    *,
    screen_command: bool,
    deterministic: list[Any],
    planned: list[Any],
) -> list[Any]:
    """Keep pane CRUD when the model plans something else for a pane command."""
    if not screen_command or not planned:
        return planned
    if any(getattr(action, "type", "") in PANE_CRUD_TYPES for action in planned):
        return planned
    pane_actions = [
        action for action in deterministic if getattr(action, "type", "") in PANE_CRUD_TYPES
    ]
    if not pane_actions:
        return planned
    rest = [
        action
        for action in planned
        if getattr(action, "type", "") not in {"page.edit", "page.create"}
    ]
    return pane_actions + rest
