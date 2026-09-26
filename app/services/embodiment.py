"""Ephemeral agent context, semantic plans, and APEX receipts.

Persists only context snapshots, versioned plans, and receipts — never poses,
gaze samples, or world coordinates. The lightweight planner emits allowlisted
``attend`` intents; invalid output falls back to deterministic attend.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from typing import Any

from fastapi import HTTPException

from app.agent_kv import get_doc, increment_counter, new_id, now_iso, put_doc, query_eq
from app.config import get_settings
from app.schemas import (
    CAPABILITY_ATTEND,
    CAPABILITY_FOCUS_TARGET,
    CAPABILITY_GAZE_SPEAKER,
    CAPABILITY_GESTURE,
    CAPABILITY_PLAN_V2,
    AgentContextSnapshot,
    AgentPlanEnvelope,
    AttendIntent,
    AvatarStepAttend,
    AvatarStepFocusTarget,
    AvatarStepGesture,
    EmbodimentReceipt,
    reject_spatial_payload,
)
from app.services import panes as panes_svc
from app.services.models_catalog import embodiment_model_id
from app.services.workspaces import require_workspace_access
from app.timeutil import expires_at_from_seconds, parse_iso

logger = logging.getLogger("vektral.embodiment")

MAX_FOCUSED = 8
_SOCIAL_RE = re.compile(
    r"\b(wave|greet|hello|hi there|look at me|point|gesture|turn|present|"
    r"introduce|bow|nod|talk to|speak to|come here|follow me)\b",
    re.I,
)
_ATTEND_RE = re.compile(
    r"\b(walk|go to|come here|come closer|follow me|look at|attend|approach|"
    r"stand (by|near|in front)|face (the|this|me)|head over|move (over|to|closer|around)|"
    r"go look|walk over|walk around|come to)\b",
    re.I,
)
_EDIT_RE = re.compile(
    r"\b(make|change|edit|fix|add|remove|delete|update|refactor|implement|"
    r"rewrite|restyle|recolor|button|css|commit|code|copy|headline|hero color)\b",
    re.I,
)
_PLANNER_SYSTEM = (
    "You are Vektral's lightweight embodiment planner. "
    "Translate the task into a semantic APEX attend intent. "
    "Respond ONLY with a JSON object:\n"
    "{\n"
    '  "pane_ids": ["pane_..."],\n'
    '  "primary_pane_id": "pane_...",\n'
    '  "phase_hint": "acting",\n'
    '  "clip_hint": "walk",\n'
    '  "gaze_hint": "primary",\n'
    '  "speech_hint": ""\n'
    "}\n"
    "Rules:\n"
    "- pane_ids MUST be a non-empty subset of the provided focused pane ids.\n"
    "- phase_hint: idle|listening|thinking|speaking|acting|confirming\n"
    "- clip_hint: walk|work|point|talk|idle\n"
    "- gaze_hint: primary|cycle|speaker\n"
    "- Never emit coordinates, Vec3, x/y/z, bones, matrices, move_to, "
    "lookAt, transforms, or arbitrary method names.\n"
)


def _ctx_id(workspace_id: str, uid: str) -> str:
    return f"embctx_{workspace_id}_{uid}"


def _plan_id_key(workspace_id: str, uid: str) -> str:
    return f"embplan_{workspace_id}_{uid}"


def _receipt_id(workspace_id: str, uid: str) -> str:
    return f"embrecv_{workspace_id}_{uid}"


def _plan_expired(expires_at: str) -> bool:
    dt = parse_iso(expires_at)
    if dt is None:
        return True
    return datetime.now(timezone.utc) >= dt


def _context_expired(doc: dict[str, Any] | None) -> bool:
    if not doc:
        return True
    return _plan_expired(str(doc.get("expires_at") or ""))


def next_plan_revision(workspace_id: str, uid: str) -> int:
    return increment_counter(
        "embodiment_revisions", f"embrev_{workspace_id}_{uid}", "revision"
    )


def filter_avatar_steps(
    steps: list[AvatarStepAttend | AvatarStepFocusTarget | AvatarStepGesture],
    capabilities: list[str] | None,
) -> tuple[list[AvatarStepAttend | AvatarStepFocusTarget | AvatarStepGesture], list[str]]:
    """Drop steps the host did not advertise. Empty capabilities = attend-only legacy."""
    caps = {str(c).strip() for c in (capabilities or []) if str(c).strip()}
    out: list[AvatarStepAttend | AvatarStepFocusTarget | AvatarStepGesture] = []
    warnings: list[str] = []
    v2 = CAPABILITY_PLAN_V2 in caps
    allow_attend = (not caps) or v2 or CAPABILITY_ATTEND in caps
    allow_focus = (not caps) or v2 or CAPABILITY_FOCUS_TARGET in caps
    for step in steps:
        if step.type == "attend":
            if not allow_attend:
                warnings.append("attend blocked: missing locomotion.attend")
                continue
            if step.gaze_hint == "speaker" and caps and not (
                v2 or CAPABILITY_GAZE_SPEAKER in caps
            ):
                step = step.model_copy(update={"gaze_hint": "primary"})
            out.append(step)
            continue
        if step.type == "focus_target":
            if not allow_focus:
                warnings.append("focus_target blocked: missing capability")
                continue
            out.append(step)
            continue
        needed = CAPABILITY_GESTURE.get(step.gesture, "")
        if not caps or not (v2 or needed in caps):
            warnings.append(f"gesture.{step.gesture} blocked: unsupported capability")
            continue
        out.append(step)
    return out, warnings


def _first_attend_intent(
    steps: list[AvatarStepAttend | AvatarStepFocusTarget | AvatarStepGesture],
) -> AttendIntent | None:
    for step in steps:
        if step.type == "attend":
            return AttendIntent(
                type="attend",
                pane_ids=list(step.pane_ids),
                primary_pane_id=step.primary_pane_id or step.pane_ids[0],
                phase_hint=step.phase_hint,
                clip_hint=step.clip_hint,
                gaze_hint=step.gaze_hint,
                speech_hint=step.speech_hint,
            )
    return None


def enabled() -> bool:
    return bool(get_settings().embodiment_enabled)


def is_attend_only_command(text: str) -> bool:
    """True when speech is locomotion/social attend, not a product edit."""
    raw = (text or "").strip()
    if not raw:
        return False
    if _EDIT_RE.search(raw):
        return False
    return bool(_ATTEND_RE.search(raw) or _SOCIAL_RE.search(raw))


def _preview_surface(pane: dict[str, Any] | None) -> bool:
    if not pane or panes_svc.is_chat_pane(pane):
        return False
    return True


def _fallback_preview_focus(workspace_id: str) -> tuple[list[str], str]:
    sess = get_doc("preview_sessions", workspace_id) or {}
    pid = str(sess.get("default_preview_pane_id") or "")
    pane = panes_svc.find_pane(pid) if pid else None
    if pane and pane.get("workspace_id") == workspace_id and _preview_surface(pane):
        return [pid], pid
    for doc in query_eq("panes", "workspace_id", workspace_id):
        if doc.get("deleted") or not _preview_surface(doc):
            continue
        if str(doc.get("workspace_id") or "") != workspace_id:
            continue
        found = str(doc.get("id") or "")
        if found:
            return [found], found
    return [], ""


def _require_panes(workspace_id: str, pane_ids: list[str]) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    missing: list[str] = []
    seen: set[str] = set()
    for raw in pane_ids:
        pid = str(raw or "").strip()
        if not pid or pid in seen:
            continue
        seen.add(pid)
        pane = panes_svc.find_pane(pid)
        if pane is None or pane.get("workspace_id") != workspace_id:
            missing.append(pid)
            continue
        found.append(pane)
    if missing:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "invalid_pane",
                "message": "pane not in workspace",
                "pane_ids": missing,
            },
        )
    return found


def validate_focused_ids(
    workspace_id: str,
    focused_pane_ids: list[str],
    primary_pane_id: str = "",
    extra_ids: list[str] | None = None,
) -> tuple[list[str], str]:
    ordered: list[str] = []
    seen: set[str] = set()
    for raw in list(focused_pane_ids or []) + ([primary_pane_id] if primary_pane_id else []):
        pid = str(raw or "").strip()
        if not pid or pid in seen:
            continue
        seen.add(pid)
        ordered.append(pid)
    if extra_ids:
        for raw in extra_ids:
            pid = str(raw or "").strip()
            if pid and pid not in seen:
                seen.add(pid)
                ordered.append(pid)
    if len(ordered) > MAX_FOCUSED:
        ordered = ordered[:MAX_FOCUSED]
    _require_panes(workspace_id, ordered)
    primary = (primary_pane_id or "").strip()
    if primary and primary not in {p for p in ordered}:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "invalid_pane",
                "message": "primary_pane_id is not in the focused set",
            },
        )
    if not primary and ordered:
        primary = ordered[0]
    return ordered, primary


def put_context(
    workspace_id: str,
    uid: str,
    snapshot: AgentContextSnapshot,
) -> dict[str, Any]:
    require_workspace_access(workspace_id, uid)
    payload = snapshot.model_dump()
    try:
        reject_spatial_payload(payload)
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail={"error": "invalid_payload", "message": str(exc)},
        ) from exc

    focused, primary = validate_focused_ids(
        workspace_id,
        list(snapshot.focused_pane_ids),
        snapshot.primary_pane_id,
    )
    _require_panes(workspace_id, [v.pane_id for v in snapshot.visible_panes])
    prev = get_doc("embodiment_context", _ctx_id(workspace_id, uid))
    # A reloaded client restarts client_revision at 1. An expired snapshot must
    # not keep rejecting that forever; only an unexpired newer revision is stale.
    if prev and _context_expired(prev):
        prev = None
    prev_client = int((prev or {}).get("client_revision") or 0)
    if snapshot.client_revision < prev_client:
        raise HTTPException(
            status_code=409,
            detail={
                "error": "stale_revision",
                "message": "client_revision is behind the stored context",
                "client_revision": prev_client,
            },
        )
    server_revision = int((prev or {}).get("server_revision") or 0) + 1
    s = get_settings()
    now = now_iso()
    doc = {
        "id": _ctx_id(workspace_id, uid),
        "workspace_id": workspace_id,
        "owner_uid": uid,
        "client_revision": snapshot.client_revision,
        "server_revision": server_revision,
        "primary_pane_id": primary,
        "focused_pane_ids": focused,
        "visible_panes": [v.model_dump() for v in snapshot.visible_panes],
        "npc_phase": snapshot.npc_phase,
        "locomotion_substate": snapshot.locomotion_substate,
        "capabilities": list(snapshot.capabilities),
        "image_refs": list(snapshot.image_refs),
        "visible_text": snapshot.visible_text,
        "preview": snapshot.preview.model_dump() if snapshot.preview else None,
        "updated_at": now,
        "expires_at": expires_at_from_seconds(s.embodiment_context_ttl_seconds),
    }
    put_doc("embodiment_context", doc["id"], doc)
    return _context_view(doc)


def get_context(workspace_id: str, uid: str) -> dict[str, Any] | None:
    doc = get_doc("embodiment_context", _ctx_id(workspace_id, uid))
    if not doc or _context_expired(doc):
        return None
    return doc


def _context_view(doc: dict[str, Any]) -> dict[str, Any]:
    return {
        "workspace_id": doc.get("workspace_id") or "",
        "client_revision": int(doc.get("client_revision") or 0),
        "server_revision": int(doc.get("server_revision") or 0),
        "primary_pane_id": doc.get("primary_pane_id") or "",
        "focused_pane_ids": list(doc.get("focused_pane_ids") or []),
        "visible_panes": list(doc.get("visible_panes") or []),
        "npc_phase": doc.get("npc_phase") or "",
        "locomotion_substate": doc.get("locomotion_substate") or "",
        "capabilities": list(doc.get("capabilities") or []),
        "image_refs": list(doc.get("image_refs") or []),
        "visible_text": doc.get("visible_text") or "",
        "preview": doc.get("preview") if isinstance(doc.get("preview"), dict) else None,
        "updated_at": doc.get("updated_at") or "",
        "expires_at": doc.get("expires_at") or "",
    }


def get_plan_doc(workspace_id: str, uid: str) -> dict[str, Any] | None:
    doc = get_doc("embodiment_plans", _plan_id_key(workspace_id, uid))
    if not doc:
        return None
    return doc


def _plan_view(doc: dict[str, Any] | None) -> dict[str, Any] | None:
    if not doc:
        return None
    if _plan_expired(str(doc.get("expires_at") or "")) and not doc.get("cancelled"):
        return None
    return {
        "plan_id": doc.get("plan_id") or "",
        "revision": int(doc.get("revision") or 0),
        "workspace_id": doc.get("workspace_id") or "",
        "actor_uid": doc.get("actor_uid") or "",
        "expires_at": doc.get("expires_at") or "",
        "job_id": doc.get("job_id") or "",
        "turn_id": doc.get("turn_id") or "",
        "cancelled": bool(doc.get("cancelled")),
        "intent": doc.get("intent") or {},
        "steps": list(doc.get("steps") or []),
        "apex_intent": doc.get("apex_intent") or {},
    }


def get_state(workspace_id: str, uid: str) -> dict[str, Any]:
    require_workspace_access(workspace_id, uid)
    ctx = get_context(workspace_id, uid)
    plan = _plan_view(get_plan_doc(workspace_id, uid))
    receipt = get_doc("embodiment_receipts", _receipt_id(workspace_id, uid))
    return {
        "enabled": enabled(),
        "context": _context_view(ctx) if ctx else None,
        "context_revision": int((ctx or {}).get("client_revision") or 0),
        "plan": plan,
        "last_receipt": (
            {
                "plan_id": receipt.get("plan_id"),
                "revision": receipt.get("revision"),
                "status": receipt.get("status"),
                "actual_primary_pane_id": receipt.get("actual_primary_pane_id") or "",
                "blocked_reason": receipt.get("blocked_reason") or "",
                "context_revision": int(receipt.get("context_revision") or 0),
                "step_id": receipt.get("step_id") or "",
                "step_index": int(receipt.get("step_index") or -1),
                "updated_at": receipt.get("updated_at") or "",
            }
            if receipt
            else None
        ),
    }


def _store_plan(envelope: AgentPlanEnvelope) -> dict[str, Any]:
    dumped = envelope.model_dump()
    dumped["apex_intent"] = envelope.apex_intent()
    dumped["id"] = _plan_id_key(envelope.workspace_id, envelope.actor_uid)
    dumped["owner_uid"] = envelope.actor_uid
    dumped["updated_at"] = now_iso()
    put_doc("embodiment_plans", dumped["id"], dumped)
    return dumped


def deterministic_attend(
    *,
    workspace_id: str,
    uid: str,
    pane_ids: list[str],
    primary_pane_id: str = "",
    job_id: str = "",
    phase_hint: str = "acting",
    clip_hint: str = "walk",
    gaze_hint: str = "primary",
    speech_hint: str = "",
) -> dict[str, Any] | None:
    if not pane_ids:
        return None
    prev = get_plan_doc(workspace_id, uid) or {}
    revision = next_plan_revision(workspace_id, uid)
    if revision <= int(prev.get("revision") or 0):
        revision = int(prev.get("revision") or 0) + 1
    s = get_settings()
    intent = AttendIntent(
        type="attend",
        pane_ids=pane_ids,
        primary_pane_id=primary_pane_id or pane_ids[0],
        phase_hint=phase_hint,  # type: ignore[arg-type]
        clip_hint=clip_hint,  # type: ignore[arg-type]
        gaze_hint=gaze_hint,  # type: ignore[arg-type]
        speech_hint=speech_hint,
    )
    envelope = AgentPlanEnvelope(
        plan_id=new_id("plan_"),
        revision=revision,
        workspace_id=workspace_id,
        actor_uid=uid,
        expires_at=expires_at_from_seconds(max(15, s.embodiment_plan_ttl_seconds)),
        job_id=job_id,
        cancelled=False,
        intent=intent,
    )
    return _store_plan(envelope)


def publish_semantic_plan(
    *,
    workspace_id: str,
    uid: str,
    steps: list[AvatarStepAttend | AvatarStepFocusTarget | AvatarStepGesture],
    capabilities: list[str] | None = None,
    job_id: str = "",
    turn_id: str = "",
) -> tuple[dict[str, Any] | None, list[str]]:
    """Persist a multi-step AvatarPlanV2. Supersedes the previous revision."""
    filtered, warnings = filter_avatar_steps(steps, capabilities)
    if not filtered:
        return None, warnings or ["avatar plan had no supported steps"]
    pane_ids: list[str] = []
    for step in filtered:
        if step.type == "attend":
            pane_ids.extend(step.pane_ids)
        elif step.type == "focus_target":
            pane_ids.append(step.pane_id)
        elif step.pane_id:
            pane_ids.append(step.pane_id)
    unique = list(dict.fromkeys(pid for pid in pane_ids if pid))
    if unique:
        _require_panes(workspace_id, unique)
    prev = get_plan_doc(workspace_id, uid) or {}
    revision = next_plan_revision(workspace_id, uid)
    if revision <= int(prev.get("revision") or 0):
        revision = int(prev.get("revision") or 0) + 1
    s = get_settings()
    intent = _first_attend_intent(filtered)
    envelope = AgentPlanEnvelope(
        plan_id=new_id("plan_"),
        revision=revision,
        workspace_id=workspace_id,
        actor_uid=uid,
        expires_at=expires_at_from_seconds(max(15, s.embodiment_plan_ttl_seconds)),
        job_id=job_id,
        turn_id=turn_id,
        cancelled=False,
        intent=intent,
        steps=filtered,
    )
    return _store_plan(envelope), warnings


def _needs_planner(text: str, pane_ids: list[str]) -> bool:
    if len(pane_ids) > 1:
        return True
    return bool(_SOCIAL_RE.search(text or ""))


async def _planner_attend(
    *,
    text: str,
    pane_ids: list[str],
    primary_pane_id: str,
    visible_text: str = "",
) -> AttendIntent | None:
    from app.services.coding import _llm_chat, extract_edit_plan

    user = (
        f"Command: {text}\n"
        f"Focused pane ids (ordered): {pane_ids}\n"
        f"Primary pane id: {primary_pane_id}\n"
        f"Visible text excerpt: {(visible_text or '')[:1500]}\n"
        "Pick attend order and hints. Do not invent pane ids."
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
    raw_ids = parsed.get("pane_ids") or parsed.get("paneIds") or []
    if not isinstance(raw_ids, list):
        return None
    allowed = {p: i for i, p in enumerate(pane_ids)}
    ordered: list[str] = []
    for item in raw_ids:
        pid = str(item or "").strip()
        if pid in allowed and pid not in ordered:
            ordered.append(pid)
    if not ordered:
        return None
    primary = str(
        parsed.get("primary_pane_id") or parsed.get("primaryPaneId") or ""
    ).strip()
    if primary not in ordered:
        primary = ordered[0]
    try:
        return AttendIntent(
            type="attend",
            pane_ids=ordered,
            primary_pane_id=primary,
            phase_hint=parsed.get("phase_hint") or parsed.get("phaseHint") or "acting",
            clip_hint=parsed.get("clip_hint") or parsed.get("clipHint") or "walk",
            gaze_hint=parsed.get("gaze_hint") or parsed.get("gazeHint") or "primary",
            speech_hint=str(parsed.get("speech_hint") or parsed.get("speechHint") or "")[
                :280
            ],
        )
    except Exception:  # noqa: BLE001
        return None


def _command_focus(
    uid: str,
    workspace_id: str,
    command: dict[str, Any],
) -> tuple[list[str], str, dict[str, Any] | None]:
    ctx = get_context(workspace_id, uid)
    cmd_focused = [str(p) for p in (command.get("focused_pane_ids") or []) if str(p)]
    cmd_primary = str(command.get("primary_pane_id") or "").strip()
    cmd_rev = int(command.get("context_revision") or 0)
    if cmd_focused:
        focused, primary = validate_focused_ids(workspace_id, cmd_focused, cmd_primary)
        return focused, primary, ctx
    if ctx:
        if cmd_rev and cmd_rev < int(ctx.get("client_revision") or 0):
            logger.info(
                "command context_revision %s behind stored %s; using command snapshot if any",
                cmd_rev,
                ctx.get("client_revision"),
            )
        focused = [str(p) for p in (ctx.get("focused_pane_ids") or []) if str(p)]
        primary = str(ctx.get("primary_pane_id") or "")
        if focused:
            focused, primary = validate_focused_ids(workspace_id, focused, primary)
            return focused, primary, ctx
    pane_id = str(command.get("pane_id") or "").strip()
    if pane_id:
        pane = panes_svc.find_pane(pane_id)
        if pane and pane.get("workspace_id") == workspace_id and not panes_svc.is_chat_pane(pane):
            return [pane_id], pane_id, ctx
    fallback, fallback_primary = _fallback_preview_focus(workspace_id)
    if fallback:
        return fallback, fallback_primary, ctx
    return [], "", ctx


def screen_context_for_prompt(
    uid: str,
    workspace_id: str,
    command: dict[str, Any],
) -> str:
    """Structured screen/focus block for the robust reasoner. No coordinates."""
    try:
        focused, primary, ctx = _command_focus(uid, workspace_id, command)
    except HTTPException:
        return ""
    if not focused and not ctx:
        return ""
    lines = ["Focused preview panes (visual/spatial targets, distinct from chat history):"]
    for pid in focused:
        pane = panes_svc.find_pane(pid)
        title = (pane or {}).get("title") or pid
        kind = panes_svc.pane_kind(pane)
        route = (pane or {}).get("route") or ""
        marker = " [primary]" if pid == primary else ""
        lines.append(f"- {pid}{marker}: {title} kind={kind} route={route}")
    if ctx:
        vis = str(ctx.get("visible_text") or "").strip()
        if vis:
            lines.append(f"Visible text/accessibility excerpt:\n{vis[:2000]}")
        refs = [str(u) for u in (ctx.get("image_refs") or []) if str(u)]
        if refs:
            lines.append("Authorized screen image refs (short-lived): " + ", ".join(refs[:4]))
        rev = int(command.get("context_revision") or ctx.get("client_revision") or 0)
        lines.append(f"Context revision: {rev}")
    return "\n".join(lines)


def image_refs_for_command(
    uid: str,
    workspace_id: str,
    command: dict[str, Any],
) -> list[str]:
    ctx = get_context(workspace_id, uid)
    if not ctx:
        return []
    return [str(u) for u in (ctx.get("image_refs") or []) if str(u)][:4]


async def plan_for_command(
    uid: str,
    workspace_id: str,
    job: dict[str, Any],
    command: dict[str, Any],
) -> dict[str, Any] | None:
    """Build a versioned APEX plan. Never fails the coding job."""
    if not enabled() or not workspace_id:
        return None
    try:
        focused, primary, ctx = _command_focus(uid, workspace_id, command)
    except HTTPException:
        focused, primary, ctx = [], "", get_context(workspace_id, uid)
        if ctx:
            focused = [str(p) for p in (ctx.get("focused_pane_ids") or []) if str(p)]
            primary = str(ctx.get("primary_pane_id") or "")
            if focused:
                try:
                    focused, primary = validate_focused_ids(
                        workspace_id, focused, primary
                    )
                except HTTPException:
                    focused, primary = [], ""
    if not focused:
        logger.info(
            "embodiment skipped; no focused or default preview panes workspace=%s",
            workspace_id,
        )
        return None

    text = str(command.get("text") or job.get("command_text") or "")
    visible = str((ctx or {}).get("visible_text") or "")
    intent: AttendIntent | None = None
    if _needs_planner(text, focused):
        try:
            intent = await _planner_attend(
                text=text,
                pane_ids=focused,
                primary_pane_id=primary,
                visible_text=visible,
            )
        except Exception:  # noqa: BLE001
            logger.exception("embodiment planner failed; using deterministic attend")
            intent = None
    if intent is None:
        return deterministic_attend(
            workspace_id=workspace_id,
            uid=uid,
            pane_ids=focused,
            primary_pane_id=primary,
            job_id=str(job.get("id") or ""),
        )
    prev = get_plan_doc(workspace_id, uid) or {}
    revision = next_plan_revision(workspace_id, uid)
    if revision <= int(prev.get("revision") or 0):
        revision = int(prev.get("revision") or 0) + 1
    s = get_settings()
    envelope = AgentPlanEnvelope(
        plan_id=new_id("plan_"),
        revision=revision,
        workspace_id=workspace_id,
        actor_uid=uid,
        expires_at=expires_at_from_seconds(max(15, s.embodiment_plan_ttl_seconds)),
        job_id=str(job.get("id") or ""),
        cancelled=False,
        intent=intent,
    )
    return _store_plan(envelope)


def post_receipt(
    workspace_id: str,
    uid: str,
    receipt: EmbodimentReceipt,
) -> dict[str, Any]:
    require_workspace_access(workspace_id, uid)
    payload = receipt.model_dump()
    try:
        reject_spatial_payload(payload)
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail={"error": "invalid_payload", "message": str(exc)},
        ) from exc

    plan = get_plan_doc(workspace_id, uid)
    if plan is None:
        raise HTTPException(
            status_code=409,
            detail={"error": "stale_plan", "message": "no active embodiment plan"},
        )
    if receipt.plan_id != str(plan.get("plan_id") or ""):
        raise HTTPException(
            status_code=409,
            detail={"error": "stale_plan", "message": "plan_id does not match current plan"},
        )
    current_rev = int(plan.get("revision") or 0)
    if receipt.revision != current_rev:
        raise HTTPException(
            status_code=409,
            detail={
                "error": "stale_plan",
                "message": "revision does not match current plan",
                "revision": current_rev,
            },
        )
    if receipt.actual_primary_pane_id:
        _require_panes(workspace_id, [receipt.actual_primary_pane_id])

    now = now_iso()
    doc = {
        "id": _receipt_id(workspace_id, uid),
        "workspace_id": workspace_id,
        "owner_uid": uid,
        "plan_id": receipt.plan_id,
        "revision": receipt.revision,
        "status": receipt.status,
        "actual_primary_pane_id": receipt.actual_primary_pane_id,
        "blocked_reason": receipt.blocked_reason,
        "context_revision": receipt.context_revision,
        "step_id": receipt.step_id,
        "step_index": receipt.step_index,
        "updated_at": now,
    }
    put_doc("embodiment_receipts", doc["id"], doc)
    if receipt.status in ("cancelled", "blocked"):
        plan["cancelled"] = True
        plan["updated_at"] = now
        put_doc("embodiment_plans", plan["id"], plan)
        job_id = str(plan.get("job_id") or "")
        if job_id:
            job = get_doc("jobs", job_id)
            if job is not None:
                raw = job.get("embodiment_json") or ""
                if isinstance(raw, dict):
                    payload = raw
                else:
                    try:
                        payload = json.loads(raw) if raw else {}
                    except (json.JSONDecodeError, TypeError):
                        payload = {}
                if isinstance(payload, dict) and payload:
                    payload["cancelled"] = True
                    job["embodiment_json"] = json.dumps(payload)
                    job["updated_at"] = now
                    put_doc("jobs", job_id, job)
    return {
        "ok": True,
        "plan_id": receipt.plan_id,
        "revision": receipt.revision,
        "status": receipt.status,
        "cancelled": bool(plan.get("cancelled")),
    }


def cancel_current_plan(workspace_id: str, uid: str, reason: str = "cancelled") -> dict[str, Any] | None:
    require_workspace_access(workspace_id, uid)
    plan = get_plan_doc(workspace_id, uid)
    if not plan or plan.get("cancelled"):
        return _plan_view(plan)
    plan["cancelled"] = True
    plan["updated_at"] = now_iso()
    put_doc("embodiment_plans", plan["id"], plan)
    put_doc(
        "embodiment_receipts",
        _receipt_id(workspace_id, uid),
        {
            "id": _receipt_id(workspace_id, uid),
            "workspace_id": workspace_id,
            "owner_uid": uid,
            "plan_id": plan.get("plan_id"),
            "revision": plan.get("revision"),
            "status": "cancelled",
            "blocked_reason": reason[:240],
            "updated_at": plan["updated_at"],
        },
    )
    return _plan_view(plan)
