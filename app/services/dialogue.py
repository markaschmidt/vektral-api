"""Dialogue-orchestrated pane, avatar, and product-job actions.

Only finalized ``role=user`` turns authorize pane/content/Linear/research
mutations. Assistant turns may emit ``noop`` or avatar behavior, and may
continue an already-authorized user action via ``continuation_of_turn_id``.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import threading
from typing import Any

from fastapi import HTTPException
from pydantic import TypeAdapter, ValidationError

from app.agent_kv import create_if_absent, get_doc, increment_counter, now_iso, put_doc, query_eq
from app.config import get_settings
from app.services.voice_actions import CHEAP_ACTION_TYPES as _CHEAP
from app.services.voice_actions import QUEUED_ACTION_TYPES as _QUEUED
from app.schemas import (
    ASSISTANT_DEFAULT_TYPES,
    USER_MUTATION_TYPES,
    AvatarStepAttend,
    AvatarStepFocusTarget,
    AvatarStepGesture,
    DialogueTurnRequestV1,
    DialogueTurnResultV1,
    PaneCaptureIn,
    PlaybackEventRequest,
    TurnActionAvatarPlan,
    TurnActionLinearRun,
    TurnActionNoop,
    TurnActionPageCreate,
    TurnActionPageEdit,
    TurnActionPaneClose,
    TurnActionPaneOpen,
    TurnActionPaneSetRoute,
    TurnActionPaneUpdate,
    TurnActionResearchRun,
    TurnActionV1,
    reject_spatial_payload,
)
from app.services import embodiment as emb_svc
from app.services import panes as panes_svc
from app.services.linear_jobs import is_linear_command
from app.services.models_catalog import embodiment_model_id, resolve_openrouter_model
from app.services.research import is_research_command
from app.services.source_locators import creatable_destinations, normalize_route
from app.services.workspaces import require_workspace_access

logger = logging.getLogger("vektral.dialogue")

_ACTION_ADAPTER: TypeAdapter[TurnActionV1] = TypeAdapter(TurnActionV1)
_TURN_LOCKS: dict[str, threading.Lock] = {}
_TURN_LOCKS_GUARD = threading.Lock()
# Strong refs so background coding jobs are not garbage-collected mid-run.
_BACKGROUND_JOBS: set[asyncio.Task[Any]] = set()
_BACKGROUND_TYPES = frozenset({"text", "voice", "code", "edit", "agent"})


def _run_in_background(job: dict[str, Any], uid: str) -> None:
    from app.services import jobs as jobs_svc

    async def _run() -> None:
        try:
            await jobs_svc.execute_queued_job(job, uid)
        except Exception:  # noqa: BLE001
            logger.exception("background job %s failed", job.get("id"))

    task = asyncio.create_task(_run())
    _BACKGROUND_JOBS.add(task)
    task.add_done_callback(_BACKGROUND_JOBS.discard)

_NOOP_RE = re.compile(
    r"^(ok|okay|thanks|thank you|yeah|yep|sure|got it|cool|nice|alright|"
    r"hello|hi|hey)[\s.!?]*$",
    re.I,
)
_ROUTE_ON_SCREEN_RE = re.compile(
    r"\b(?:show|display|switch(?:\s+to)?|navigate(?:\s+to)?|go(?:\s+to)?|open)\s+"
    r"(?:the\s+)?(?P<target>/[A-Za-z0-9/_-]+|[A-Za-z][\w-]*)\s+"
    r"(?:on|in)\s+(?:this|the|that)\s+(?:screen|pane|display)\b",
    re.I,
)
_SET_ROUTE_RE = re.compile(
    r"\b(?:set|change|point)\s+(?:this\s+)?(?:screen|pane|display)\s+"
    r"(?:to|at)\s+(?P<target>/[A-Za-z0-9/_-]+|[A-Za-z][\w-]*)\b",
    re.I,
)
_PAGE_CREATE_RE = re.compile(
    r"\b(?:create|add|make)\s+(?:a\s+)?(?:new\s+)?(?:page|route)\b",
    re.I,
)
_SOCIAL_GESTURE_RE = re.compile(
    r"\b(?P<g>wave|nod|point|present)\b",
    re.I,
)
_PLANNER_SYSTEM = (
    "You are Vektral's lightweight dialogue planner. "
    "Translate the finalized utterance into an ordered JSON action plan.\n"
    "Respond ONLY with JSON:\n"
    '{"summary":"...","actions":[{"type":"noop|avatar.plan|pane.open|pane.set_route|'
    'page.edit|page.create|research.run|linear.run","action_id":"a1","depends_on":[],'
    "...fields}]}\n"
    "Rules:\n"
    "- pane.open: title, route, optional force_new, optional kind blank|preview|chat. "
    "kind blank is an empty screen. Never invent pane ids.\n"
    "- pane.update: same pane_id. kind converts it in place "
    "(blank, chat, or preview) and route or external_url is injected.\n"
    "- pane.set_route: existing preview pane_id + route. A blank pane becomes a page.\n"
    "- page.edit / page.create: natural-language instruction, never file paths.\n"
    "- avatar.plan steps: attend | focus_target | gesture(wave|nod|point|present). "
    "Each step may have cue immediate|speech_start|speech_end. No coordinates, "
    "bones, transforms, clip filenames, or move_to.\n"
    "- Use noop when the utterance does not need a workspace mutation or motion.\n"
    "- depends_on must refer to earlier action_id values.\n"
)

STAGE_SHADOW = "shadow"
STAGE_USER_PANE_AVATAR = "user_pane_avatar"
STAGE_QUEUED_EDITS = "queued_edits"
STAGE_ASSISTANT_AVATAR = "assistant_avatar"


def orchestration_enabled() -> bool:
    return bool(get_settings().dialogue_orchestration_enabled)


def rollout_stage() -> str:
    return str(get_settings().dialogue_orchestration_stage or STAGE_SHADOW)


def _lock_for(key: str) -> threading.Lock:
    with _TURN_LOCKS_GUARD:
        lock = _TURN_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _TURN_LOCKS[key] = lock
        return lock


def _turn_key(workspace_id: str, uid: str, turn_id: str) -> str:
    raw = f"{workspace_id}\0{uid}\0{(turn_id or '').strip()}".encode("utf-8")
    return f"dlgturn_{hashlib.sha256(raw).hexdigest()}"


def _latest_key(workspace_id: str, uid: str) -> str:
    return f"dlglatest_{workspace_id}_{uid}"


def _rev_key(workspace_id: str, uid: str) -> str:
    return f"dlgrev_{workspace_id}_{uid}"


def _playback_key(workspace_id: str, uid: str) -> str:
    return f"dlgplay_{workspace_id}_{uid}"


def next_turn_revision(workspace_id: str, uid: str) -> int:
    return increment_counter("dialogue_revisions", _rev_key(workspace_id, uid), "revision")


def _safe_dump(payload: Any) -> dict[str, Any]:
    if hasattr(payload, "model_dump"):
        return payload.model_dump()
    if isinstance(payload, dict):
        return dict(payload)
    return {}


def _action_id(prefix: str, index: int) -> str:
    return f"{prefix}{index}"


def get_turn(workspace_id: str, uid: str, turn_id: str) -> dict[str, Any] | None:
    require_workspace_access(workspace_id, uid)
    doc = get_doc("dialogue_turns", _turn_key(workspace_id, uid, turn_id))
    if not doc or doc.get("owner_uid") != uid:
        return None
    result = doc.get("result")
    return result if isinstance(result, dict) else None


def list_recent_turns(workspace_id: str, uid: str, limit: int = 8) -> list[dict[str, Any]]:
    docs = [
        d
        for d in query_eq("dialogue_turns", "workspace_id", workspace_id)
        if d.get("owner_uid") == uid
    ]
    docs.sort(key=lambda d: d.get("created_at") or "")
    return docs[-max(1, min(limit, 8)) :]


def latest_turn_event(workspace_id: str, uid: str) -> dict[str, Any] | None:
    doc = get_doc("dialogue_latest", _latest_key(workspace_id, uid))
    if not doc or doc.get("owner_uid") != uid:
        return None
    return doc


def _publish_latest(workspace_id: str, uid: str, result: dict[str, Any]) -> None:
    put_doc(
        "dialogue_latest",
        _latest_key(workspace_id, uid),
        {
            "id": _latest_key(workspace_id, uid),
            "workspace_id": workspace_id,
            "owner_uid": uid,
            "turn_id": result.get("turn_id") or "",
            "revision": int(result.get("revision") or 0),
            "status": result.get("status") or "",
            "action_ids": [
                str(a.get("action_id") or "")
                for a in (result.get("actions") or [])
                if isinstance(a, dict)
            ],
            "pane_ids": [
                str(p.get("id") or "")
                for p in list(result.get("spawned_panes") or [])
                + list(result.get("updated_panes") or [])
                if isinstance(p, dict)
            ],
            "job_ids": list(result.get("job_ids") or []),
            "updated_at": now_iso(),
        },
    )


def record_playback_event(
    workspace_id: str,
    uid: str,
    body: PlaybackEventRequest,
) -> dict[str, Any]:
    """Persist coarse TTS lifecycle; never raises into the TTS path."""
    require_workspace_access(workspace_id, uid)
    digest = hashlib.sha256(
        f"{workspace_id}\0{uid}\0{body.event_id}".encode("utf-8")
    ).hexdigest()
    doc_id = f"pbev_{digest}"
    payload = {
        "id": doc_id,
        "event_id": body.event_id,
        "turn_id": body.turn_id,
        "type": body.type,
        "workspace_id": workspace_id,
        "owner_uid": uid,
        "created_at": now_iso(),
    }
    created, stored = create_if_absent("playback_events", doc_id, payload)
    if created:
        bucket_id = _playback_key(workspace_id, uid)
        bucket = get_doc("dialogue_playback", bucket_id) or {}
        events = list(bucket.get("events") or [])
        events.append(
            {
                "event_id": body.event_id,
                "turn_id": body.turn_id,
                "type": body.type,
                "created_at": payload["created_at"],
            }
        )
        put_doc(
            "dialogue_playback",
            bucket_id,
            {
                "id": bucket_id,
                "workspace_id": workspace_id,
                "owner_uid": uid,
                "events": events[-32:],
                "updated_at": now_iso(),
            },
        )
    return {
        "ok": True,
        "duplicate": not created,
        "event_id": stored.get("event_id") or body.event_id,
        "turn_id": stored.get("turn_id") or body.turn_id,
        "type": stored.get("type") or body.type,
    }


def playback_events_since(
    workspace_id: str, uid: str, after_event_id: str
) -> list[dict[str, Any]]:
    doc = get_doc("dialogue_playback", _playback_key(workspace_id, uid))
    events = list((doc or {}).get("events") or [])
    if after_event_id == "__init__":
        return []
    if not after_event_id:
        return events
    out: list[dict[str, Any]] = []
    seen = False
    for ev in events:
        if not seen:
            if str(ev.get("event_id") or "") == after_event_id:
                seen = True
            continue
        out.append(ev)
    return out


def latest_playback_event_id(workspace_id: str, uid: str) -> str:
    doc = get_doc("dialogue_playback", _playback_key(workspace_id, uid))
    events = list((doc or {}).get("events") or [])
    if not events:
        return ""
    return str(events[-1].get("event_id") or "")


def _pane_catalog(workspace_id: str, uid: str) -> list[dict[str, Any]]:
    try:
        panes = panes_svc.list_panes(workspace_id, uid)
    except Exception:  # noqa: BLE001
        return []
    return [
        {
            "id": p.get("id"),
            "title": p.get("title"),
            "route": p.get("route"),
            "kind": p.get("kind"),
            "source_kind": p.get("source_kind"),
        }
        for p in panes
    ]


def _pending_jobs(workspace_id: str, uid: str) -> list[dict[str, str]]:
    from app.services import jobs as jobs_svc

    out: list[dict[str, str]] = []
    for job in jobs_svc.list_workspace_jobs(workspace_id):
        if job.get("owner_uid") and job.get("owner_uid") != uid:
            continue
        status = str(job.get("status") or "")
        if status in {"ready", "failed", "completed"}:
            continue
        out.append(
            {
                "id": str(job.get("id") or ""),
                "status": status,
                "command_type": str(job.get("command_type") or ""),
            }
        )
        if len(out) >= 8:
            break
    return out


def build_context(
    uid: str,
    workspace_id: str,
    body: DialogueTurnRequestV1,
) -> dict[str, Any]:
    ctx = emb_svc.get_context(workspace_id, uid)
    focused = list(body.focused_pane_ids or [])
    primary = body.primary_pane_id
    if not focused and ctx:
        focused = [str(p) for p in (ctx.get("focused_pane_ids") or []) if str(p)]
        primary = primary or str(ctx.get("primary_pane_id") or "")
    capabilities = list((ctx or {}).get("capabilities") or [])
    recent = list_recent_turns(workspace_id, uid, limit=8)
    history = [
        {
            "role": d.get("role") or "user",
            "text": str(d.get("text") or "")[:500],
            "turn_id": d.get("turn_id") or d.get("id") or "",
        }
        for d in recent
        if d.get("turn_id") != body.turn_id
    ]
    chat_id = body.conversation_pane_id
    try:
        resolved = panes_svc.resolve_chat_pane(
            uid,
            workspace_id,
            pane_id=str(chat_id or ""),
            new_conversation=bool(body.new_conversation),
        )
        chat_id = str(resolved.get("chat_pane_id") or chat_id or "")
    except Exception:  # noqa: BLE001
        logger.debug("chat pane resolve skipped")
    return {
        "panes": _pane_catalog(workspace_id, uid),
        "focused_pane_ids": focused,
        "primary_pane_id": primary,
        "capabilities": capabilities,
        "context_revision": body.context_revision
        or int((ctx or {}).get("client_revision") or 0),
        "history": history[-8:],
        "pending_jobs": _pending_jobs(workspace_id, uid),
        "chat_pane_id": chat_id,
        "agent_context": ctx,
    }


def _normalize_target_route(raw: str) -> str:
    token = (raw or "").strip()
    if not token:
        return ""
    return panes_svc._normalize_route(token) or normalize_route(token)  # noqa: SLF001


def _gesture_from_text(text: str) -> str:
    match = _SOCIAL_GESTURE_RE.search(text or "")
    if not match:
        return ""
    name = (match.group("g") or "").lower()
    if name in {"wave", "nod", "point", "present"}:
        return name
    return ""


def _needs_llm(text: str, actions: list[TurnActionV1]) -> bool:
    kinds = {a.type for a in actions}
    if kinds & {"pane.open", "pane.set_route", "pane.update", "pane.close"} and kinds <= {
        "pane.open",
        "pane.set_route",
        "pane.update",
        "pane.close",
        "avatar.plan",
        "page.edit",
    }:
        return False
    if "pane.open" in kinds and ("page.edit" in kinds or "avatar.plan" in kinds):
        return False
    if len(actions) >= 3:
        return True
    if panes_svc.is_screen_command(text) and (
        emb_svc.is_attend_only_command(text) or bool(_EDIT_HINT.search(text))
    ):
        return True
    if "," in text and (" and " in text.lower()):
        return True
    return False


_EDIT_HINT = re.compile(
    r"\b(make|change|edit|fix|add|remove|update|rewrite|restyle|recolor|button|headline)\b",
    re.I,
)


def _deterministic_actions(
    *,
    role: str,
    text: str,
    context: dict[str, Any],
) -> list[TurnActionV1]:
    actions: list[TurnActionV1] = []
    n = 1
    focused = [str(p) for p in (context.get("focused_pane_ids") or []) if str(p)]
    primary = str(context.get("primary_pane_id") or (focused[0] if focused else ""))

    if role == "assistant":
        gesture = _gesture_from_text(text)
        if gesture:
            steps: list[Any] = [
                AvatarStepGesture(
                    type="gesture",
                    step_id="s1",
                    cue="speech_start",
                    gesture=gesture,  # type: ignore[arg-type]
                )
            ]
            if primary:
                steps.insert(
                    0,
                    AvatarStepAttend(
                        type="attend",
                        step_id="s0",
                        cue="immediate",
                        pane_ids=[primary],
                        primary_pane_id=primary,
                    ),
                )
            actions.append(
                TurnActionAvatarPlan(
                    type="avatar.plan",
                    action_id=_action_id("a", n),
                    steps=steps,
                )
            )
            return actions
        if emb_svc.is_attend_only_command(text) and (focused or primary):
            pid = primary or focused[0]
            actions.append(
                TurnActionAvatarPlan(
                    type="avatar.plan",
                    action_id=_action_id("a", n),
                    steps=[
                        AvatarStepAttend(
                            type="attend",
                            step_id="s1",
                            pane_ids=focused or [pid],
                            primary_pane_id=pid,
                        )
                    ],
                )
            )
            return actions
        actions.append(TurnActionNoop(type="noop", action_id=_action_id("a", n)))
        return actions

    if _NOOP_RE.match((text or "").strip()):
        return [TurnActionNoop(type="noop", action_id="a1")]

    if panes_svc.is_pane_close_command(text):
        target = panes_svc.resolve_pane_target(
            str(context.get("workspace_id") or ""),
            text,
            focused=focused,
            primary=primary,
            panes=list(context.get("panes") or []),
        )
        hint = panes_svc._named_screen_token(text)  # noqa: SLF001
        actions.append(
            TurnActionPaneClose(
                type="pane.close",
                action_id=_action_id("a", n),
                pane_id=str((target or {}).get("id") or ""),
                title=str((target or {}).get("title") or hint),
            )
        )
        return actions

    if panes_svc.is_pane_update_command(text):
        parsed = panes_svc.parse_pane_update(text)
        target = panes_svc.resolve_pane_target(
            str(context.get("workspace_id") or ""),
            text,
            focused=focused,
            primary=primary,
            panes=list(context.get("panes") or []),
        )
        actions.append(
            TurnActionPaneUpdate(
                type="pane.update",
                action_id=_action_id("a", n),
                pane_id=str((target or {}).get("id") or ""),
                title=str(parsed.get("title") or ""),
                route=str(parsed.get("route") or ""),
                kind=str(parsed.get("kind") or ""),
                source_kind=str(parsed.get("source_kind") or ""),
                external_url=str(parsed.get("external_url") or ""),
            )
        )
        return actions

    rebind = _ROUTE_ON_SCREEN_RE.search(text) or _SET_ROUTE_RE.search(text)
    if rebind and primary:
        route = _normalize_target_route(rebind.group("target"))
        if route:
            actions.append(
                TurnActionPaneSetRoute(
                    type="pane.set_route",
                    action_id=_action_id("a", n),
                    pane_id=primary,
                    route=route,
                )
            )
            n += 1
            actions.append(
                TurnActionAvatarPlan(
                    type="avatar.plan",
                    action_id=_action_id("a", n),
                    depends_on=[actions[0].action_id],
                    steps=[
                        AvatarStepAttend(
                            type="attend",
                            step_id="s1",
                            pane_ids=[primary],
                            primary_pane_id=primary,
                        )
                    ],
                )
            )
            return actions

    if is_research_command(text):
        return [
            TurnActionResearchRun(
                type="research.run",
                action_id="a1",
                query=text,
            )
        ]
    if is_linear_command(text):
        return [
            TurnActionLinearRun(
                type="linear.run",
                action_id="a1",
                instruction=text,
            )
        ]

    page_create = bool(_PAGE_CREATE_RE.search(text)) and not panes_svc.is_screen_command(
        text
    )
    if page_create:
        specs = panes_svc.parse_screen_targets(text) or [{"title": "", "route": ""}]
        spec = specs[0]
        route = str(spec.get("route") or "") or _normalize_target_route(text)
        actions.append(
            TurnActionPageCreate(
                type="page.create",
                action_id=_action_id("a", n),
                title=str(spec.get("title") or ""),
                route=route or "/page",
                instruction=text,
            )
        )
        return actions

    if panes_svc.is_screen_command(text):
        specs = panes_svc.parse_screen_targets(text) or [{"title": "Screen", "route": ""}]
        open_ids: list[str] = []
        for spec in specs:
            aid = _action_id("a", n)
            actions.append(
                TurnActionPaneOpen(
                    type="pane.open",
                    action_id=aid,
                    title=str(spec.get("title") or ""),
                    route=str(spec.get("route") or ""),
                    force_new=str(spec.get("force_new") or "") == "1",
                    kind=str(spec.get("kind") or ""),
                )
            )
            open_ids.append(aid)
            n += 1
        if emb_svc.is_attend_only_command(text) or _SOCIAL_GESTURE_RE.search(text):
            gesture = _gesture_from_text(text)
            steps = [
                AvatarStepAttend(
                    type="attend",
                    step_id="s1",
                    cue="immediate",
                    pane_ids=focused or (["$pane.open"] if open_ids else []),
                    primary_pane_id=primary,
                )
            ]
            if gesture:
                steps.append(
                    AvatarStepGesture(
                        type="gesture",
                        step_id="s2",
                        cue="speech_start",
                        gesture=gesture,  # type: ignore[arg-type]
                    )
                )
            actions.append(
                TurnActionAvatarPlan(
                    type="avatar.plan",
                    action_id=_action_id("a", n),
                    depends_on=list(open_ids),
                    steps=steps,
                )
            )
        return actions

    if emb_svc.is_attend_only_command(text):
        fallback, fallback_primary = emb_svc._fallback_preview_focus(  # noqa: SLF001
            str(context.get("workspace_id") or "")
        )
        ids = focused or fallback
        pid = primary or fallback_primary
        if not ids:
            return [TurnActionNoop(type="noop", action_id="a1")]
        gesture = _gesture_from_text(text)
        steps_a: list[Any] = [
            AvatarStepAttend(
                type="attend",
                step_id="s1",
                pane_ids=ids,
                primary_pane_id=pid or ids[0],
            )
        ]
        if len(ids) > 1:
            steps_a.append(
                AvatarStepFocusTarget(
                    type="focus_target",
                    step_id="s2",
                    cue="speech_start",
                    pane_id=pid or ids[0],
                )
            )
        if gesture:
            steps_a.append(
                AvatarStepGesture(
                    type="gesture",
                    step_id=f"s{len(steps_a) + 1}",
                    cue="speech_start",
                    gesture=gesture,  # type: ignore[arg-type]
                    pane_id=pid or ids[0],
                )
            )
        return [
            TurnActionAvatarPlan(
                type="avatar.plan",
                action_id="a1",
                steps=steps_a,
            )
        ]

    # Only an explicit edit verb starts a coding job. Filler speech ("ah", "also",
    # "any of the other screens") is a noop, not a multi-minute page edit.
    if _EDIT_HINT.search(text) and len((text or "").split()) >= 3:
        return [
            TurnActionPageEdit(
                type="page.edit",
                action_id="a1",
                pane_id=primary,
                instruction=text,
            )
        ]
    return [TurnActionNoop(type="noop", action_id="a1")]


async def _llm_actions(
    text: str,
    context: dict[str, Any],
) -> list[TurnActionV1] | None:
    from app.services.coding import _llm_chat, extract_edit_plan

    panes = context.get("panes") or []
    user = (
        f"Utterance: {text}\n"
        f"Pane catalog: {panes[:24]}\n"
        f"Focused pane ids: {context.get('focused_pane_ids')}\n"
        f"Primary pane id: {context.get('primary_pane_id')}\n"
        f"Capabilities: {context.get('capabilities')}\n"
        f"Recent turns: {context.get('history')}\n"
        "Do not invent pane ids. Prefer noop over coding when the user is only chatting."
    )
    llm = await _llm_chat(
        model=embodiment_model_id(),
        system=_PLANNER_SYSTEM,
        user=user,
    )
    if not bool(llm.get("ok")):
        return None
    parsed = extract_edit_plan(str(llm.get("content") or ""))
    if not isinstance(parsed, dict):
        return None
    try:
        reject_spatial_payload(parsed)
    except ValueError:
        return None
    raw_actions = parsed.get("actions")
    if not isinstance(raw_actions, list):
        return None
    out: list[TurnActionV1] = []
    for index, item in enumerate(raw_actions[:16], start=1):
        if not isinstance(item, dict):
            continue
        item = dict(item)
        item.setdefault("action_id", _action_id("a", index))
        try:
            out.append(_ACTION_ADAPTER.validate_python(item))
        except ValidationError:
            continue
    return out or None


def _validate_dependencies(actions: list[TurnActionV1]) -> None:
    seen: set[str] = set()
    for action in actions:
        aid = action.action_id
        if aid in seen:
            raise HTTPException(
                status_code=400,
                detail={"error": "invalid_plan", "message": f"duplicate action_id {aid}"},
            )
        for dep in action.depends_on:
            if dep not in seen:
                raise HTTPException(
                    status_code=400,
                    detail={
                        "error": "invalid_plan",
                        "message": f"{aid} depends on unknown {dep}",
                    },
                )
        seen.add(aid)


def _validate_role_policy(
    *,
    role: str,
    actions: list[TurnActionV1],
    continuation: dict[str, Any] | None,
) -> list[TurnActionV1]:
    if role == "user":
        return actions
    allowed = set(ASSISTANT_DEFAULT_TYPES)
    if continuation:
        allowed.update(str(t) for t in (continuation.get("authorized_mutation_types") or []))
    kept: list[TurnActionV1] = []
    for action in actions:
        if action.type in allowed:
            kept.append(action)
            continue
        if action.type in USER_MUTATION_TYPES:
            continue
        kept.append(action)
    if not kept:
        kept = [TurnActionNoop(type="noop", action_id="a1")]
    return kept


def _filter_by_stage(
    *,
    role: str,
    actions: list[TurnActionV1],
    stage: str,
) -> tuple[list[TurnActionV1], list[str], list[TurnActionV1]]:
    """Return (executable, warnings, leftover_for_legacy)."""
    warnings: list[str] = []
    executable: list[TurnActionV1] = []
    leftover: list[TurnActionV1] = []
    if stage == STAGE_SHADOW:
        return [], ["shadow: plan logged, not executed"], list(actions)
    for action in actions:
        if action.type in _CHEAP:
            if action.type == "avatar.plan" and role == "assistant" and stage != STAGE_ASSISTANT_AVATAR:
                warnings.append("assistant avatar deferred until assistant_avatar stage")
                leftover.append(action)
                continue
            executable.append(action)
            continue
        if action.type in _QUEUED:
            if stage in {STAGE_QUEUED_EDITS, STAGE_ASSISTANT_AVATAR}:
                executable.append(action)
            else:
                leftover.append(action)
                warnings.append(f"{action.type} deferred until queued_edits stage")
            continue
        leftover.append(action)
    return executable, warnings, leftover


def _hydrate_avatar_steps(
    steps: list[Any],
    spawned: list[dict[str, Any]],
    focused: list[str],
    primary: str,
) -> list[Any]:
    opened = [str(p.get("id") or "") for p in spawned if p.get("id")]
    out: list[Any] = []
    for index, step in enumerate(steps, start=1):
        data = step.model_dump() if hasattr(step, "model_dump") else dict(step)
        data.setdefault("step_id", f"s{index}")
        if data.get("type") == "attend":
            ids = [str(x) for x in (data.get("pane_ids") or []) if str(x) and not str(x).startswith("$")]
            if not ids:
                ids = opened or focused
            data["pane_ids"] = ids[:8]
            if not data.get("primary_pane_id"):
                data["primary_pane_id"] = primary or (ids[0] if ids else "")
            if not data["pane_ids"]:
                continue
            out.append(AvatarStepAttend.model_validate(data))
        elif data.get("type") == "focus_target":
            pid = str(data.get("pane_id") or "")
            if not pid or pid.startswith("$"):
                pid = primary or (opened[0] if opened else "")
            if not pid:
                continue
            data["pane_id"] = pid
            out.append(AvatarStepFocusTarget.model_validate(data))
        elif data.get("type") == "gesture":
            out.append(AvatarStepGesture.model_validate(data))
    return out


async def _queue_job(
    uid: str,
    workspace_id: str,
    *,
    command_type: str,
    text: str,
    pane_id: str,
    route: str,
    model: str,
    chat_pane_id: str,
    focused_pane_ids: list[str],
    primary_pane_id: str,
    context_revision: int,
    captures: list[dict[str, Any]],
    create_paths: list[str] | None = None,
    skip_execute: bool = True,
) -> dict[str, Any]:
    from app.services import jobs as jobs_svc

    resolved = resolve_openrouter_model(model)
    use_model = resolved.get("model") or "" if resolved.get("ok") == "true" else ""
    job = jobs_svc.create_job_record(
        owner_uid=uid,
        status="queued",
        command_type=command_type,
        command_text=text,
        workspace_id=workspace_id,
        pane_id=pane_id,
        chat_pane_id=chat_pane_id,
        route=route or "/",
        model=use_model,
    )
    command = {
        "type": command_type,
        "text": text,
        "workspace_id": workspace_id,
        "pane_id": pane_id,
        "route": route,
        "focused_pane_ids": focused_pane_ids,
        "primary_pane_id": primary_pane_id,
        "context_revision": context_revision,
        "captures": captures,
    }
    if command_type in {"text", "edit", "code", "agent"}:
        from fastapi import HTTPException as FastAPIHTTPException

        from app.services.captures import pair_captures_to_targets
        from app.services.coding_targets import resolve_coding_targets

        try:
            snap = await resolve_coding_targets(workspace_id, uid, command)
        except FastAPIHTTPException as exc:
            detail = exc.detail if isinstance(exc.detail, dict) else {"error": str(exc.detail)}
            job["mutation_error_code"] = str(detail.get("error") or "NO_CODING_TARGETS")
            job["error"] = str(detail.get("message") or job["mutation_error_code"])
            job["status"] = "failed"
            job["updated_at"] = now_iso()
            put_doc("jobs", job["id"], job)
            return job
        if create_paths:
            snap = dict(snap)
            snap["create_paths"] = list(create_paths)
        job["coding_targets"] = snap
        job["create_paths"] = list(create_paths or [])
        job["base_commit_sha"] = str(snap.get("base_commit_sha") or "")
        paired, reasons = pair_captures_to_targets(
            uid=uid,
            workspace_id=workspace_id,
            targets=list(snap.get("targets") or []),
            captures=captures,
            context_revision=int(snap.get("context_revision") or 0),
        )
        job["captures"] = paired
        job["capture_audit"] = reasons
        job["updated_at"] = now_iso()
        put_doc("jobs", job["id"], job)
    if not skip_execute:
        return await jobs_svc.execute_queued_job(job, uid)
    put_doc("jobs", job["id"], job)
    return job


async def execute_actions(
    *,
    uid: str,
    workspace_id: str,
    role: str,
    text: str,
    actions: list[TurnActionV1],
    context: dict[str, Any],
    body: DialogueTurnRequestV1,
    execute: bool,
) -> dict[str, Any]:
    spawned: list[dict[str, Any]] = []
    updated: list[dict[str, Any]] = []
    job_ids: list[str] = []
    warnings: list[str] = []
    avatar_plan: dict[str, Any] | None = None
    summary_parts: list[str] = []
    focused = [str(p) for p in (context.get("focused_pane_ids") or []) if str(p)]
    primary = str(context.get("primary_pane_id") or "")
    chat_id = str(context.get("chat_pane_id") or "")
    capabilities = list(context.get("capabilities") or [])

    if not execute:
        return {
            "spawned_panes": spawned,
            "updated_panes": updated,
            "job_ids": job_ids,
            "avatar_plan": None,
            "warnings": warnings,
            "summary": "shadow plan",
        }

    for action in actions:
        # TEMP-LOG: per-action trace. Remove once pane CRUD is hiccup-free.
        try:
            logger.info(
                "[voice %s] execute %s pane_id=%s title=%r route=%r",
                getattr(body, "turn_id", "?"),
                getattr(action, "type", "?"),
                getattr(action, "pane_id", ""),
                getattr(action, "title", ""),
                getattr(action, "route", ""),
            )
        except Exception:  # noqa: BLE001
            pass
        if action.type == "noop":
            summary_parts.append("No workspace change.")
            continue
        if action.type == "pane.open":
            opened = panes_svc.open_structured_pane(
                workspace_id,
                uid,
                title=action.title,
                route=action.route,
                force_new=action.force_new,
                kind=action.kind,
            )
            (spawned if opened.get("created") else updated).append(opened)
            pid = str(opened.get("id") or "")
            if pid:
                focused = [pid] + [p for p in focused if p != pid]
                primary = pid
            summary_parts.append(
                f"{'Created' if opened.get('created') else 'Opened'} "
                f"{opened.get('title') or opened.get('route')}."
            )
            continue
        if action.type == "pane.set_route":
            view = panes_svc.set_preview_route(
                workspace_id, uid, action.pane_id, action.route
            )
            updated.append(view)
            primary = str(view.get("id") or primary)
            summary_parts.append(f"Pointed the screen at {view.get('route')}.")
            continue
        if action.type == "pane.update":
            pid = action.pane_id or ""
            if not pid:
                target = panes_svc.resolve_pane_target(
                    workspace_id,
                    text,
                    focused=focused,
                    primary=primary,
                    panes=list(context.get("panes") or []),
                )
                pid = str((target or {}).get("id") or "")
            view = panes_svc.update_pane(
                workspace_id,
                pid,
                uid,
                title=action.title,
                route=action.route,
                kind=action.kind,
                source_kind=action.source_kind,
                external_url=action.external_url,
            )
            if view is None:
                warnings.append("no matching pane to update")
                summary_parts.append("No matching pane to update.")
                continue
            updated.append(view)
            primary = str(view.get("id") or primary)
            summary_parts.append(
                f"Updated {view.get('title') or view.get('route')}."
            )
            continue
        if action.type == "pane.close":
            pid = action.pane_id or ""
            if not pid:
                target = panes_svc.resolve_pane_target(
                    workspace_id,
                    text,
                    focused=focused,
                    primary=primary,
                    panes=list(context.get("panes") or []),
                )
                pid = str((target or {}).get("id") or "")
            if not pid:
                warnings.append("no matching pane to close")
                summary_parts.append("No matching pane to close.")
                continue
            closed = panes_svc.delete_pane(workspace_id, pid, uid)
            if not closed.get("ok"):
                warnings.append(str(closed.get("message") or "close failed"))
                summary_parts.append("No matching pane to close.")
                continue
            updated.append({"id": pid, "deleted": True, "title": action.title})
            focused = [p for p in focused if p != pid]
            if primary == pid:
                primary = focused[0] if focused else ""
            summary_parts.append(f"Removed {action.title or pid}.")
            continue
        if action.type == "avatar.plan":
            if not emb_svc.enabled():
                warnings.append("embodiment disabled; avatar plan skipped")
                continue
            steps = _hydrate_avatar_steps(list(action.steps), spawned, focused, primary)
            plan, plan_warnings = emb_svc.publish_semantic_plan(
                workspace_id=workspace_id,
                uid=uid,
                steps=steps,
                capabilities=capabilities,
                turn_id=body.turn_id,
            )
            warnings.extend(plan_warnings)
            avatar_plan = plan
            if plan:
                summary_parts.append("Avatar plan published.")
            continue
        if action.type == "page.create":
            opened = panes_svc.open_structured_pane(
                workspace_id,
                uid,
                title=action.title,
                route=action.route,
                force_new=True,
            )
            spawned.append(opened)
            pid = str(opened.get("id") or "")
            route = str(opened.get("route") or action.route or "/")
            sess = get_doc("preview_sessions", workspace_id) or {}
            runtime = sess.get("runtime") if isinstance(sess.get("runtime"), dict) else {}
            kind = str(runtime.get("kind") or "vite-spa")
            workdir = str(runtime.get("workdir") or ".")
            create_paths = creatable_destinations(kind, route, workdir)
            job = await _queue_job(
                uid,
                workspace_id,
                command_type="text",
                text=action.instruction or text,
                pane_id=pid,
                route=route,
                model=body.model,
                chat_pane_id=chat_id,
                focused_pane_ids=[pid] if pid else focused,
                primary_pane_id=pid or primary,
                context_revision=int(context.get("context_revision") or 0),
                captures=[c.model_dump() for c in body.captures],
                create_paths=create_paths,
            )
            job_ids.append(str(job.get("id") or ""))
            summary_parts.append(f"Queued source creation for {route}.")
            continue
        if action.type == "page.edit":
            pane_id = action.pane_id or primary
            pane = panes_svc.find_pane(pane_id) if pane_id else None
            route = str((pane or {}).get("route") or "/")
            job = await _queue_job(
                uid,
                workspace_id,
                command_type="text",
                text=action.instruction or text,
                pane_id=pane_id or chat_id,
                route=route,
                model=body.model,
                chat_pane_id=chat_id,
                focused_pane_ids=focused or ([pane_id] if pane_id else []),
                primary_pane_id=primary or pane_id,
                context_revision=int(context.get("context_revision") or 0),
                captures=[c.model_dump() for c in body.captures],
            )
            job_ids.append(str(job.get("id") or ""))
            summary_parts.append("Queued a page edit.")
            continue
        if action.type == "research.run":
            job = await _queue_job(
                uid,
                workspace_id,
                command_type="research",
                text=action.query or text,
                pane_id=chat_id,
                route="/",
                model=body.model,
                chat_pane_id=chat_id,
                focused_pane_ids=focused,
                primary_pane_id=primary,
                context_revision=int(context.get("context_revision") or 0),
                captures=[],
            )
            job_ids.append(str(job.get("id") or ""))
            summary_parts.append("Queued research.")
            continue
        if action.type == "linear.run":
            job = await _queue_job(
                uid,
                workspace_id,
                command_type="linear",
                text=action.instruction or text,
                pane_id=chat_id,
                route="/",
                model=body.model,
                chat_pane_id=chat_id,
                focused_pane_ids=focused,
                primary_pane_id=primary,
                context_revision=int(context.get("context_revision") or 0),
                captures=[],
            )
            job_ids.append(str(job.get("id") or ""))
            summary_parts.append("Queued a Linear job.")
            continue

    _ = role
    return {
        "spawned_panes": spawned,
        "updated_panes": updated,
        "job_ids": job_ids,
        "avatar_plan": avatar_plan,
        "warnings": warnings,
        "summary": " ".join(summary_parts).strip(),
    }


async def plan_actions(
    *,
    role: str,
    text: str,
    context: dict[str, Any],
) -> tuple[list[TurnActionV1], list[str]]:
    warnings: list[str] = []
    actions = _deterministic_actions(role=role, text=text, context=context)
    # TEMP-LOG: planner trace. Remove once pane CRUD is hiccup-free.
    logger.info(
        "[voice %s] plan role=%s text=%r deterministic=%s",
        str(context.get("turn_id") or "?"),
        role,
        (text or "")[:160],
        [getattr(a, "type", "?") for a in actions],
    )
    if _needs_llm(text, actions):
        try:
            planned = await _llm_actions(text, context)
        except Exception:  # noqa: BLE001
            logger.exception("dialogue planner failed; using deterministic actions")
            planned = None
        if planned:
            from app.services.voice_actions import merge_planned_actions

            merged = merge_planned_actions(
                screen_command=panes_svc.is_screen_command(text)
                or panes_svc.is_pane_close_command(text)
                or panes_svc.is_pane_update_command(text),
                deterministic=actions,
                planned=planned,
            )
            if merged is not planned:
                warnings.append("pane command kept deterministic pane action")
            actions = merged
            logger.info(
                "[voice %s] plan llm=%s merged=%s",
                str(context.get("turn_id") or "?"),
                [getattr(a, "type", "?") for a in (planned or [])],
                [getattr(a, "type", "?") for a in actions],
            )
        else:
            warnings.append("planner fallback to deterministic actions")
    if not actions:
        actions = [TurnActionNoop(type="noop", action_id="a1")]
    return actions, warnings


async def submit_turn(
    uid: str,
    workspace_id: str,
    body: DialogueTurnRequestV1,
) -> dict[str, Any]:
    require_workspace_access(workspace_id, uid)
    payload = body.model_dump()
    try:
        reject_spatial_payload(payload)
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail={"error": "invalid_payload", "message": str(exc)},
        ) from exc
    if not body.is_final:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "partial_utterance",
                "message": "only finalized turns are accepted",
            },
        )

    key = _turn_key(workspace_id, uid, body.turn_id)
    now = now_iso()
    create_if_absent(
        "dialogue_turns",
        key,
        {
            "id": key,
            "workspace_id": workspace_id,
            "owner_uid": uid,
            "turn_id": body.turn_id,
            "role": body.role,
            "text": body.text,
            "source": body.source,
            "created_at": now,
            "status": "reserved",
        },
    )
    with _lock_for(key):
        existing = get_doc("dialogue_turns", key)
        if existing and existing.get("result"):
            result = dict(existing["result"])
            result["duplicate"] = True
            return result

        stage = rollout_stage()
        context = build_context(uid, workspace_id, body)
        context["workspace_id"] = workspace_id
        context["turn_id"] = body.turn_id
        # TEMP-LOG: turn trace. Remove once pane CRUD is hiccup-free.
        logger.info(
            "[voice %s] turn role=%s stage=%s text=%r",
            body.turn_id,
            body.role,
            stage,
            (body.text or "")[:160],
        )
        continuation = None
        if body.continuation_of_turn_id:
            continuation = get_turn(workspace_id, uid, body.continuation_of_turn_id)

        if body.role == "user" and stage != STAGE_SHADOW:
            try:
                emb_svc.cancel_current_plan(
                    workspace_id, uid, reason="superseded_by_turn"
                )
            except Exception:  # noqa: BLE001
                logger.debug("no prior plan to cancel")

        actions, plan_warnings = await plan_actions(
            role=body.role, text=body.text, context=context
        )
        try:
            _validate_dependencies(actions)
        except HTTPException:
            actions = [TurnActionNoop(type="noop", action_id="a1")]
            plan_warnings.append("invalid planner dependencies; fell back to noop")
        actions = _validate_role_policy(
            role=body.role, actions=actions, continuation=continuation
        )
        executable, stage_warnings, leftover = _filter_by_stage(
            role=body.role, actions=actions, stage=stage
        )
        # TEMP-LOG: action trace. Remove once pane CRUD is hiccup-free.
        logger.info(
            "[voice %s] actions=%s executable=%s leftover=%s warnings=%s",
            body.turn_id,
            [getattr(a, "type", "?") for a in actions],
            [getattr(a, "type", "?") for a in executable],
            [getattr(a, "type", "?") for a in leftover],
            list(plan_warnings) + list(stage_warnings),
        )
        revision = next_turn_revision(workspace_id, uid)
        put_doc(
            "dialogue_turns",
            key,
            {
                "id": key,
                "workspace_id": workspace_id,
                "owner_uid": uid,
                "turn_id": body.turn_id,
                "role": body.role,
                "text": body.text,
                "source": body.source,
                "created_at": now,
                "revision": revision,
                "status": "planning",
            },
        )

        executed = await execute_actions(
            uid=uid,
            workspace_id=workspace_id,
            role=body.role,
            text=body.text,
            actions=executable,
            context=context,
            body=body,
            execute=stage != STAGE_SHADOW,
        )
        authorized = sorted(
            {a.type for a in actions if a.type in USER_MUTATION_TYPES}
        )
        status = "shadow" if stage == STAGE_SHADOW else "completed"
        if all(a.type == "noop" for a in actions):
            status = "noop" if stage != STAGE_SHADOW else "shadow"
        result_model = DialogueTurnResultV1(
            turn_id=body.turn_id,
            revision=revision,
            duplicate=False,
            status=status,  # type: ignore[arg-type]
            role=body.role,
            actions=actions,
            avatar_plan=executed.get("avatar_plan"),
            spawned_panes=list(executed.get("spawned_panes") or []),
            updated_panes=list(executed.get("updated_panes") or []),
            job_ids=list(executed.get("job_ids") or []),
            summary=str(executed.get("summary") or "")[:500],
            warnings=list(plan_warnings)
            + list(stage_warnings)
            + list(executed.get("warnings") or []),
            continuation_of_turn_id=body.continuation_of_turn_id,
            authorized_mutation_types=authorized,
        )
        result = result_model.model_dump()
        result["leftover_action_types"] = [a.type for a in leftover]
        put_doc(
            "dialogue_turns",
            key,
            {
                "id": key,
                "workspace_id": workspace_id,
                "owner_uid": uid,
                "turn_id": body.turn_id,
                "role": body.role,
                "text": body.text,
                "source": body.source,
                "created_at": now,
                "revision": revision,
                "result": result,
                "updated_at": now_iso(),
            },
        )
        _publish_latest(workspace_id, uid, result)
        if (
            stage != STAGE_SHADOW
            and body.role == "user"
            and context.get("chat_pane_id")
            and body.text
        ):
            try:
                panes_svc.append_message(
                    str(context["chat_pane_id"]), uid, "user", body.text
                )
            except Exception:  # noqa: BLE001
                logger.debug("chat append skipped")
        if (
            body.role == "user"
            and context.get("chat_pane_id")
            and result.get("summary")
            and stage != STAGE_SHADOW
        ):
            try:
                panes_svc.append_message(
                    str(context["chat_pane_id"]),
                    uid,
                    "assistant",
                    str(result.get("summary") or ""),
                )
            except Exception:  # noqa: BLE001
                logger.debug("assistant chat append skipped")
        return result


def _turn_action_types(result: dict[str, Any]) -> list[str]:
    out: list[str] = []
    for action in result.get("actions") or []:
        if isinstance(action, dict):
            kind = str(action.get("type") or "")
        else:
            kind = str(getattr(action, "type", "") or "")
        if kind:
            out.append(kind)
    return out


def _turn_opened_panes(result: dict[str, Any]) -> list[dict[str, Any]]:
    panes: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in list(result.get("spawned_panes") or []) + list(
        result.get("updated_panes") or []
    ):
        if not isinstance(raw, dict):
            continue
        pid = str(raw.get("id") or "")
        if pid and pid in seen:
            continue
        if pid:
            seen.add(pid)
        panes.append(raw)
    return panes


def result_to_job_view(result: dict[str, Any], job: dict[str, Any] | None = None) -> dict[str, Any]:
    from app.schemas import job_view

    if job:
        view = job_view(job)
        view["dialogue_turn"] = {
            "turn_id": result.get("turn_id"),
            "revision": result.get("revision"),
            "status": result.get("status"),
        }
        return view
    plan = result.get("avatar_plan") or {}
    spawned = result.get("spawned_panes") or []
    pane_id = ""
    route = "/"
    if spawned:
        pane_id = str(spawned[0].get("id") or "")
        route = str(spawned[0].get("route") or "/")
    return {
        "id": (result.get("job_ids") or ["dlg_" + str(result.get("turn_id") or "")])[0],
        "status": "completed" if result.get("status") in {"completed", "noop", "shadow"} else "queued",
        "command_type": "voice",
        "command_text": "",
        "workspace_id": "",
        "pane_id": pane_id,
        "route": route,
        "model": "",
        "branch": "",
        "commit_sha": "",
        "base_commit_sha": "",
        "preview_url": "",
        "reload_version": 0,
        "coding_targets": {},
        "affected_pane_ids": [str(p.get("id") or "") for p in spawned if p.get("id")],
        "mutation_error_code": "",
        "vision_status": "",
        "captures": [],
        "logs": "",
        "result_json": "",
        "embodiment_json": plan if isinstance(plan, dict) else {},
        "error": "",
        "created_at": now_iso(),
        "updated_at": now_iso(),
        "owner_uid": "",
        "dialogue_turn": {
            "turn_id": result.get("turn_id"),
            "revision": result.get("revision"),
            "status": result.get("status"),
        },
    }


def _stable_legacy_turn_id(uid: str, command: dict[str, Any], *, require: bool) -> str:
    explicit = str(command.get("turn_id") or command.get("turnId") or "").strip()
    if explicit:
        return explicit
    if require:
        return ""
    raw = json.dumps(
        {
            "uid": uid,
            "ws": command.get("workspace_id") or "",
            "text": str(command.get("text") or "").strip(),
            "type": command.get("type") or "",
            "pane": command.get("pane_id") or "",
            "route": command.get("route") or "",
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return "turn_" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def _captures_from_command(command: dict[str, Any]) -> list[PaneCaptureIn]:
    out: list[PaneCaptureIn] = []
    for raw in command.get("captures") or []:
        try:
            if isinstance(raw, PaneCaptureIn):
                out.append(raw)
            elif isinstance(raw, dict):
                out.append(PaneCaptureIn.model_validate(raw))
        except Exception:  # noqa: BLE001
            continue
        if len(out) >= 8:
            break
    return out


def _attach_legacy_job(
    workspace_id: str,
    uid: str,
    turn_id: str,
    result: dict[str, Any],
    job_view: dict[str, Any],
) -> None:
    key = _turn_key(workspace_id, uid, turn_id)
    existing = get_doc("dialogue_turns", key) or {}
    merged = dict(existing)
    merged["legacy_job_id"] = str(job_view.get("id") or "")
    merged["legacy_job_view"] = job_view
    result_doc = dict(result)
    jid = str(job_view.get("id") or "")
    if jid and jid not in list(result_doc.get("job_ids") or []):
        result_doc["job_ids"] = list(result_doc.get("job_ids") or []) + [jid]
    merged["result"] = result_doc
    merged["updated_at"] = now_iso()
    put_doc("dialogue_turns", key, merged)


async def submit_from_legacy_command(
    uid: str,
    command: dict[str, Any],
) -> dict[str, Any] | None:
    """Adapter entry. Returns a job view, or None to fall through to regex dispatch."""
    if not orchestration_enabled():
        return None
    workspace_id = str(command.get("workspace_id") or "")
    text = str(command.get("text") or "").strip()
    if not workspace_id or not text:
        return None
    from app.services import jobs as jobs_svc

    stage = rollout_stage()
    require_id = stage != STAGE_SHADOW
    turn_id = _stable_legacy_turn_id(uid, command, require=require_id)
    if not turn_id:
        return None

    body = DialogueTurnRequestV1(
        turn_id=turn_id,
        role="user",
        text=text,
        source="vocalbridge" if str(command.get("type") or "") == "voice" else "typed",
        conversation_pane_id=str(command.get("pane_id") or ""),
        context_revision=int(command.get("context_revision") or 0),
        primary_pane_id=str(command.get("primary_pane_id") or ""),
        focused_pane_ids=[str(p) for p in (command.get("focused_pane_ids") or []) if str(p)],
        captures=_captures_from_command(command),
        is_final=True,
        model=str(command.get("model") or ""),
        new_conversation=bool(command.get("new_conversation")),
    )
    key = _turn_key(workspace_id, uid, turn_id)
    existing = get_doc("dialogue_turns", key)
    if existing and existing.get("result") and existing.get("legacy_job_view"):
        stored = dict(existing["legacy_job_view"])
        return jobs_svc.attach_query_fields(
            stored, turn=dict(existing["result"]), duplicate=True
        )

    result = await submit_turn(uid, workspace_id, body)
    existing = get_doc("dialogue_turns", key) or existing
    if result.get("duplicate"):
        if existing and existing.get("legacy_job_view"):
            stored = dict(existing["legacy_job_view"])
            return jobs_svc.attach_query_fields(stored, turn=result, duplicate=True)
        view = existing.get("outbound_job_view") if existing else None
        if isinstance(view, dict) and view:
            return jobs_svc.attach_query_fields(dict(view), turn=result, duplicate=True)
        return jobs_svc.attach_query_fields(
            result_to_job_view(result), turn=result, duplicate=True
        )

    leftover = [str(t) for t in (result.get("leftover_action_types") or [])]

    if stage == STAGE_SHADOW:
        if existing and existing.get("legacy_job_view"):
            return jobs_svc.attach_query_fields(
                dict(existing["legacy_job_view"]), turn=result, duplicate=True
            )
        job = await jobs_svc._legacy_submit_command(uid, command)
        _attach_legacy_job(workspace_id, uid, turn_id, result, job)
        return jobs_svc.attach_query_fields(
            job, turn=result, duplicate=bool(result.get("duplicate"))
        )

    needs_legacy = any(t in _QUEUED for t in leftover)
    if needs_legacy:
        if existing and existing.get("legacy_job_view"):
            return jobs_svc.attach_query_fields(
                dict(existing["legacy_job_view"]), turn=result, duplicate=True
            )
        command["_skip_screen"] = True
        command["_skip_attend"] = True
        job = await jobs_svc._legacy_submit_command(uid, command)
        _attach_legacy_job(workspace_id, uid, turn_id, result, job)
        return jobs_svc.attach_query_fields(job, turn=result)

    opened = _turn_opened_panes(result)
    opened_ids = [str(p.get("id") or "") for p in opened if p.get("id")]
    if opened_ids:
        command["focused_pane_ids"] = opened_ids
        command["primary_pane_id"] = opened_ids[0]
    job_ids = list(result.get("job_ids") or [])
    last: dict[str, Any] | None = None
    if job_ids:
        for jid in job_ids:
            raw = get_doc("jobs", jid)
            if raw is None:
                continue
            raw = await jobs_svc.attach_embodiment_plan(uid, raw, command)
            if str(raw.get("command_type") or "text") in _BACKGROUND_TYPES:
                _run_in_background(raw, uid)
                last = raw
                continue
            last = await jobs_svc.execute_queued_job(raw, uid)
            if last is not None and not last.get("embodiment_json"):
                last = await jobs_svc.attach_embodiment_plan(uid, last, command)
    if last is not None:
        view = jobs_svc.attach_query_fields(result_to_job_view(result, last), turn=result)
        _attach_legacy_job(workspace_id, uid, turn_id, result, view)
        return view

    synthetic = result_to_job_view(result)
    synthetic["workspace_id"] = workspace_id
    synthetic["command_text"] = text
    synthetic["owner_uid"] = uid
    action_types = _turn_action_types(result)
    if "pane.open" in action_types and opened:
        synthetic["status"] = "ready"
        if opened_ids:
            synthetic["pane_id"] = opened_ids[0]
            synthetic["route"] = str(opened[0].get("route") or synthetic.get("route") or "/")
            synthetic["affected_pane_ids"] = opened_ids
        from app.services.voice_actions import open_screen_result

        synthetic["result_json"] = json.dumps(
            open_screen_result(opened, str(result.get("summary") or ""))
        )
    elif "pane.close" in action_types:
        from app.services.voice_actions import close_pane_result

        closed = [p for p in (result.get("updated_panes") or []) if p.get("deleted")]
        pid = str((closed[0].get("id") if closed else "") or "")
        synthetic["status"] = "ready"
        synthetic["affected_pane_ids"] = [pid] if pid else []
        synthetic["result_json"] = json.dumps(
            close_pane_result(
                pid,
                str((closed[0].get("title") if closed else "") or ""),
                str(result.get("summary") or ""),
            )
        )
    elif "pane.update" in action_types or "pane.set_route" in action_types:
        from app.services.voice_actions import update_pane_result

        updated = [p for p in (result.get("updated_panes") or []) if not p.get("deleted")]
        synthetic["status"] = "ready"
        if updated:
            synthetic["pane_id"] = str(updated[0].get("id") or synthetic.get("pane_id") or "")
            synthetic["route"] = str(updated[0].get("route") or synthetic.get("route") or "/")
            synthetic["affected_pane_ids"] = [str(updated[0].get("id") or "")]
            synthetic["result_json"] = json.dumps(
                update_pane_result(updated[0], str(result.get("summary") or ""))
            )
        elif result.get("summary"):
            synthetic["result_json"] = json.dumps(
                {
                    "ok": True,
                    "summary": result.get("summary"),
                    "turn_id": result.get("turn_id"),
                }
            )
    elif result.get("summary") and not synthetic.get("result_json"):
        synthetic["result_json"] = json.dumps(
            {
                "ok": True,
                "summary": result.get("summary"),
                "turn_id": result.get("turn_id"),
            }
        )
    plan = await emb_svc.plan_for_command(uid, workspace_id, synthetic, command)
    if plan:
        synthetic["embodiment_json"] = plan
    elif not synthetic.get("embodiment_json"):
        synthetic["embodiment_json"] = result.get("avatar_plan") or {}
    view = jobs_svc.attach_query_fields(synthetic, turn=result)
    _attach_legacy_job(workspace_id, uid, turn_id, result, view)
    return view
