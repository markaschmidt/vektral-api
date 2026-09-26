"""Shared Pydantic models + dict view helpers for Vektral-API."""

from __future__ import annotations

import json
from typing import Annotated, Any, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


# --- Plan C agent bodies ---


class PaneCreate(BaseModel):
    title: str = ""
    route: str = "/"
    layout_json: str = ""
    kind: str = ""
    source_kind: str = ""
    external_url: str = ""


class PaneUpdate(BaseModel):
    title: str = ""
    route: str = ""
    layout_json: str = ""
    kind: str = ""
    source_kind: str = ""
    external_url: str = ""


class PaneCaptureIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    pane_id: str = Field(min_length=1, max_length=80)
    context_revision: int = 0
    captured_at: str = ""
    image_ref: str = Field(default="", max_length=80)
    visible_text: str = Field(default="", max_length=2000)
    viewport: dict[str, Any] = Field(default_factory=dict)


class CommandBody(BaseModel):
    type: str = "text"
    text: str = ""
    workspace_id: str = ""
    pane_id: str = ""
    route: str = ""
    model: str = ""
    payload_json: str = ""
    new_conversation: bool = False
    context_revision: int = 0
    primary_pane_id: str = ""
    focused_pane_ids: list[str] = Field(default_factory=list, max_length=8)
    captures: list[PaneCaptureIn] = Field(default_factory=list, max_length=8)
    turn_id: str = ""
    is_final: bool = True
    command: dict[str, Any] | None = None


class PaneCommandBody(BaseModel):
    text: str = ""
    type: str = "text"
    model: str = ""
    payload_json: str = ""
    new_conversation: bool = False
    context_revision: int = 0
    primary_pane_id: str = ""
    focused_pane_ids: list[str] = Field(default_factory=list, max_length=8)
    captures: list[PaneCaptureIn] = Field(default_factory=list, max_length=8)
    turn_id: str = ""
    is_final: bool = True


class VocalBridgeQuery(BaseModel):
    text: str = ""
    transcript: str = ""
    workspace_id: str = ""
    pane_id: str = ""
    route: str = ""
    model: str = ""
    new_conversation: bool = False
    context_revision: int = 0
    primary_pane_id: str = ""
    focused_pane_ids: list[str] = Field(default_factory=list, max_length=8)
    captures: list[PaneCaptureIn] = Field(default_factory=list, max_length=8)
    turn_id: str = ""
    turnId: str = ""
    is_final: bool = True

    @model_validator(mode="before")
    @classmethod
    def _alias_turn_id(cls, data: Any) -> Any:
        if isinstance(data, dict):
            turn = str(data.get("turn_id") or "").strip()
            camel = str(data.get("turnId") or "").strip()
            if not turn and camel:
                data = {**data, "turn_id": camel}
        return data


# --- Embodiment / APEX semantic plans (no world coordinates) ---

_FORBIDDEN_SPATIAL = {
    "position",
    "lookat",
    "look_at",
    "vec3",
    "x",
    "y",
    "z",
    "rotation",
    "transform",
    "bone",
    "bones",
    "matrix",
}


class VisiblePaneHint(BaseModel):
    model_config = ConfigDict(extra="forbid")

    pane_id: str = Field(min_length=1, max_length=80)
    distance: Literal["", "near", "mid", "far"] = ""
    direction: Literal["", "front", "left", "right", "behind"] = ""
    visible: bool = True


class PreviewCapabilities(BaseModel):
    """Host preview presentation — isolated from avatar ``capabilities[]``."""

    model_config = ConfigDict(extra="forbid")

    container: Literal["", "iframe", "webview"] = ""
    in_world: bool = False
    interactive: bool = True
    arbitrary_origins: bool = False
    video: bool = False
    keyboard: bool = True


class AgentContextSnapshot(BaseModel):
    """Debounced semantic focus from Spatium. Never includes Vec3/transforms."""

    model_config = ConfigDict(extra="forbid")

    client_revision: int = Field(ge=1)
    primary_pane_id: str = ""
    focused_pane_ids: list[str] = Field(default_factory=list, max_length=8)
    visible_panes: list[VisiblePaneHint] = Field(default_factory=list, max_length=16)
    npc_phase: Literal[
        "", "idle", "listening", "thinking", "speaking", "acting", "confirming"
    ] = ""
    locomotion_substate: Literal["", "orienting", "navigating", "attending"] = ""
    capabilities: list[str] = Field(default_factory=list, max_length=16)
    image_refs: list[str] = Field(
        default_factory=list,
        max_length=4,
        description="Deprecated flat refs. Prefer captures[].image_ref opaque ids.",
    )
    visible_text: str = Field(default="", max_length=4000)
    captures: list[Any] = Field(default_factory=list, max_length=8)
    preview: PreviewCapabilities | None = None

    @field_validator("image_refs")
    @classmethod
    def _no_data_urls(cls, refs: list[str]) -> list[str]:
        out: list[str] = []
        for raw in refs:
            url = (raw or "").strip()
            if not url:
                continue
            if url.lower().startswith("data:"):
                raise ValueError("inline image payloads are not allowed")
            if len(url) > 512:
                raise ValueError("image_ref too long")
            out.append(url)
        return out

    @model_validator(mode="before")
    @classmethod
    def _accept_frontend_snapshot(cls, data: Any) -> Any:
        """Drop locomotion extras the VR client sends; still reject coordinates."""
        if not isinstance(data, dict):
            return data
        renamed = _rename_snapshot_keys(data)
        reject_spatial_payload(renamed)
        return _clean_snapshot(renamed)


PhaseHint = Literal[
    "idle", "listening", "thinking", "speaking", "acting", "confirming"
]
ClipHint = Literal["walk", "work", "point", "talk", "idle"]
GazeHint = Literal["primary", "cycle", "speaker"]
AvatarCue = Literal["immediate", "speech_start", "speech_end"]
GestureName = Literal["wave", "nod", "point", "present"]

CAPABILITY_PLAN_V2 = "avatar.plan.v2"
CAPABILITY_ATTEND = "locomotion.attend"
CAPABILITY_GAZE_SPEAKER = "gaze.speaker"
CAPABILITY_FOCUS_TARGET = "focus_target"
CAPABILITY_GESTURE = {
    "wave": "gesture.wave",
    "nod": "gesture.nod",
    "point": "gesture.point",
    "present": "gesture.present",
}


class AttendIntent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["attend"] = "attend"
    pane_ids: list[str] = Field(min_length=1, max_length=8)
    primary_pane_id: str = ""
    phase_hint: PhaseHint = "acting"
    clip_hint: ClipHint = "walk"
    gaze_hint: GazeHint = "primary"
    speech_hint: str = Field(default="", max_length=280)


class AvatarStepAttend(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["attend"] = "attend"
    step_id: str = Field(default="", max_length=80)
    cue: AvatarCue = "immediate"
    pane_ids: list[str] = Field(min_length=1, max_length=8)
    primary_pane_id: str = ""
    phase_hint: PhaseHint = "acting"
    clip_hint: ClipHint = "walk"
    gaze_hint: GazeHint = "primary"
    speech_hint: str = Field(default="", max_length=280)


class AvatarStepFocusTarget(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["focus_target"] = "focus_target"
    step_id: str = Field(default="", max_length=80)
    cue: AvatarCue = "immediate"
    pane_id: str = Field(min_length=1, max_length=80)
    gaze_hint: GazeHint = "primary"


class AvatarStepGesture(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["gesture"] = "gesture"
    step_id: str = Field(default="", max_length=80)
    cue: AvatarCue = "immediate"
    gesture: GestureName
    pane_id: str = ""
    phase_hint: PhaseHint = "acting"


AvatarStep = Annotated[
    Union[AvatarStepAttend, AvatarStepFocusTarget, AvatarStepGesture],
    Field(discriminator="type"),
]


class AvatarPlanV2(BaseModel):
    """Ordered semantic avatar steps. Never includes coordinates or clip files."""

    model_config = ConfigDict(extra="forbid")

    steps: list[AvatarStep] = Field(default_factory=list, max_length=16)


def _step_to_apex(step: AvatarStepAttend | AvatarStepFocusTarget | AvatarStepGesture) -> dict[str, Any]:
    if step.type == "attend":
        primary = step.primary_pane_id or (step.pane_ids[0] if step.pane_ids else "")
        payload: dict[str, Any] = {
            "type": "attend",
            "paneIds": list(step.pane_ids),
            "primaryPaneId": primary,
            "phaseHint": step.phase_hint,
            "clipHint": step.clip_hint,
            "gazeHint": step.gaze_hint,
            "speechHint": step.speech_hint,
        }
        if step.step_id:
            payload["stepId"] = step.step_id
        if step.cue != "immediate":
            payload["cue"] = step.cue
        return payload
    if step.type == "focus_target":
        payload = {
            "type": "focus_target",
            "targetId": step.pane_id,
            "gazeHint": step.gaze_hint,
        }
        if step.step_id:
            payload["stepId"] = step.step_id
        if step.cue != "immediate":
            payload["cue"] = step.cue
        return payload
    payload = {
        "type": "gesture",
        "gesture": step.gesture,
        "phaseHint": step.phase_hint,
    }
    if step.pane_id:
        payload["paneId"] = step.pane_id
    if step.step_id:
        payload["stepId"] = step.step_id
    if step.cue != "immediate":
        payload["cue"] = step.cue
    return payload


class AgentPlanEnvelope(BaseModel):
    """Versioned semantic plan. Backend never emits move_to or coordinates."""

    model_config = ConfigDict(extra="forbid")

    plan_id: str
    revision: int = Field(ge=1)
    workspace_id: str
    actor_uid: str = ""
    expires_at: str = ""
    job_id: str = ""
    turn_id: str = ""
    cancelled: bool = False
    intent: AttendIntent | None = None
    steps: list[AvatarStep] = Field(default_factory=list, max_length=16)

    @model_validator(mode="after")
    def _intent_or_steps(self) -> AgentPlanEnvelope:
        if self.intent is None and not self.steps:
            raise ValueError("intent or steps required")
        return self

    def apex_intent(self) -> dict[str, Any]:
        if self.steps:
            apex_steps = [_step_to_apex(step) for step in self.steps]
        else:
            assert self.intent is not None
            primary = self.intent.primary_pane_id or self.intent.pane_ids[0]
            apex_steps = [
                {
                    "type": "attend",
                    "paneIds": list(self.intent.pane_ids),
                    "primaryPaneId": primary,
                    "phaseHint": self.intent.phase_hint,
                    "clipHint": self.intent.clip_hint,
                    "gazeHint": self.intent.gaze_hint,
                    "speechHint": self.intent.speech_hint,
                }
            ]
        return {
            "type": "execute_plan",
            "planId": self.plan_id,
            "revision": self.revision,
            "expiresAt": self.expires_at,
            "workspaceId": self.workspace_id,
            "actorId": self.actor_uid,
            "steps": apex_steps,
        }


class EmbodimentReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid")

    plan_id: str = Field(min_length=1, max_length=80)
    revision: int = Field(ge=1)
    status: Literal["accepted", "running", "completed", "blocked", "cancelled"]
    actual_primary_pane_id: str = ""
    blocked_reason: str = Field(default="", max_length=240)
    context_revision: int = 0
    step_id: str = Field(default="", max_length=80)
    step_index: int = Field(default=-1, ge=-1, le=64)


class TurnActionNoop(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["noop"] = "noop"
    action_id: str = Field(min_length=1, max_length=80)
    depends_on: list[str] = Field(default_factory=list, max_length=16)


class TurnActionAvatarPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["avatar.plan"] = "avatar.plan"
    action_id: str = Field(min_length=1, max_length=80)
    depends_on: list[str] = Field(default_factory=list, max_length=16)
    steps: list[AvatarStep] = Field(default_factory=list, max_length=16)


class TurnActionPaneOpen(BaseModel):
    """Reference voice action. Cheap stage; result action is open_screen."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["pane.open"] = "pane.open"
    action_id: str = Field(min_length=1, max_length=80)
    depends_on: list[str] = Field(default_factory=list, max_length=16)
    title: str = Field(default="", max_length=120)
    route: str = Field(default="/", max_length=240)
    force_new: bool = False
    kind: str = Field(default="", max_length=16)


class TurnActionPaneSetRoute(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["pane.set_route"] = "pane.set_route"
    action_id: str = Field(min_length=1, max_length=80)
    depends_on: list[str] = Field(default_factory=list, max_length=16)
    pane_id: str = Field(min_length=1, max_length=80)
    route: str = Field(min_length=1, max_length=240)


class TurnActionPaneUpdate(BaseModel):
    """Rename and/or repoint an existing preview pane. Cheap stage."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["pane.update"] = "pane.update"
    action_id: str = Field(min_length=1, max_length=80)
    depends_on: list[str] = Field(default_factory=list, max_length=16)
    pane_id: str = Field(default="", max_length=80)
    title: str = Field(default="", max_length=120)
    route: str = Field(default="", max_length=240)
    kind: str = Field(default="", max_length=16)
    source_kind: str = Field(default="", max_length=32)
    external_url: str = Field(default="", max_length=1024)


class TurnActionPaneClose(BaseModel):
    """Soft-delete a preview pane. Cheap stage; result action is close_pane."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["pane.close"] = "pane.close"
    action_id: str = Field(min_length=1, max_length=80)
    depends_on: list[str] = Field(default_factory=list, max_length=16)
    pane_id: str = Field(default="", max_length=80)
    title: str = Field(default="", max_length=120)


class TurnActionPageEdit(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["page.edit"] = "page.edit"
    action_id: str = Field(min_length=1, max_length=80)
    depends_on: list[str] = Field(default_factory=list, max_length=16)
    pane_id: str = Field(default="", max_length=80)
    instruction: str = Field(default="", max_length=4000)


class TurnActionPageCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["page.create"] = "page.create"
    action_id: str = Field(min_length=1, max_length=80)
    depends_on: list[str] = Field(default_factory=list, max_length=16)
    title: str = Field(default="", max_length=120)
    route: str = Field(default="", max_length=240)
    pane_id: str = Field(default="", max_length=80)
    instruction: str = Field(default="", max_length=4000)


class TurnActionResearchRun(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["research.run"] = "research.run"
    action_id: str = Field(min_length=1, max_length=80)
    depends_on: list[str] = Field(default_factory=list, max_length=16)
    query: str = Field(default="", max_length=4000)


class TurnActionLinearRun(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["linear.run"] = "linear.run"
    action_id: str = Field(min_length=1, max_length=80)
    depends_on: list[str] = Field(default_factory=list, max_length=16)
    instruction: str = Field(default="", max_length=4000)


TurnActionV1 = Annotated[
    Union[
        TurnActionNoop,
        TurnActionAvatarPlan,
        TurnActionPaneOpen,
        TurnActionPaneSetRoute,
        TurnActionPaneUpdate,
        TurnActionPaneClose,
        TurnActionPageEdit,
        TurnActionPageCreate,
        TurnActionResearchRun,
        TurnActionLinearRun,
    ],
    Field(discriminator="type"),
]

USER_MUTATION_TYPES = frozenset(
    {
        "pane.open",
        "pane.set_route",
        "pane.update",
        "pane.close",
        "page.edit",
        "page.create",
        "research.run",
        "linear.run",
    }
)
ASSISTANT_DEFAULT_TYPES = frozenset({"noop", "avatar.plan"})


class DialogueTurnRequestV1(BaseModel):
    model_config = ConfigDict(extra="forbid")

    turn_id: str = Field(min_length=8, max_length=80)
    role: Literal["user", "assistant"]
    text: str = Field(min_length=1, max_length=8000)
    source: Literal["vocalbridge", "typed", "assistant_tts", "system"] = "vocalbridge"
    conversation_pane_id: str = Field(default="", max_length=80)
    reply_to_turn_id: str = Field(default="", max_length=80)
    continuation_of_turn_id: str = Field(default="", max_length=80)
    context_revision: int = 0
    primary_pane_id: str = Field(default="", max_length=80)
    focused_pane_ids: list[str] = Field(default_factory=list, max_length=8)
    captures: list[PaneCaptureIn] = Field(default_factory=list, max_length=8)
    is_final: bool = True
    model: str = ""
    new_conversation: bool = False

    @field_validator("turn_id")
    @classmethod
    def _turn_id_safe(cls, value: str) -> str:
        raw = (value or "").strip()
        if not raw:
            raise ValueError("turn_id is required")
        return raw


class PlaybackEventRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_id: str = Field(min_length=8, max_length=80)
    turn_id: str = Field(min_length=8, max_length=80)
    type: Literal["playback_started", "playback_finished"]


class DialogueTurnResultV1(BaseModel):
    model_config = ConfigDict(extra="forbid")

    turn_id: str
    revision: int = Field(ge=1)
    duplicate: bool = False
    status: Literal[
        "accepted",
        "planned",
        "executing",
        "completed",
        "noop",
        "rejected",
        "shadow",
    ]
    role: Literal["user", "assistant"]
    actions: list[TurnActionV1] = Field(default_factory=list, max_length=24)
    avatar_plan: dict[str, Any] | None = None
    spawned_panes: list[dict[str, Any]] = Field(default_factory=list)
    updated_panes: list[dict[str, Any]] = Field(default_factory=list)
    job_ids: list[str] = Field(default_factory=list, max_length=16)
    summary: str = ""
    warnings: list[str] = Field(default_factory=list, max_length=16)
    continuation_of_turn_id: str = ""
    authorized_mutation_types: list[str] = Field(default_factory=list)


_SNAPSHOT_CAMEL = {
    "clientRevision": "client_revision",
    "primaryPaneId": "primary_pane_id",
    "focusedPaneIds": "focused_pane_ids",
    "visiblePanes": "visible_panes",
    "npcPhase": "npc_phase",
    "locomotionSubstate": "locomotion_substate",
    "imageRefs": "image_refs",
    "visibleText": "visible_text",
    "paneId": "pane_id",
    "inWorld": "in_world",
    "arbitraryOrigins": "arbitrary_origins",
}
_SNAPSHOT_PHASES = {"", "idle", "listening", "thinking", "speaking", "acting", "confirming"}
_SNAPSHOT_SUBSTATES = {"", "orienting", "navigating", "attending"}
_SNAPSHOT_DISTANCES = {"", "near", "mid", "far"}
_SNAPSHOT_DIRECTIONS = {"", "front", "left", "right", "behind"}
_SNAPSHOT_CONTAINERS = {"", "iframe", "webview"}


def _rename_snapshot_keys(data: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, val in data.items():
        out[_SNAPSHOT_CAMEL.get(str(key), str(key))] = val
    return out


def _clean_snapshot(data: dict[str, Any]) -> dict[str, Any]:
    panes: list[dict[str, Any]] = []
    for item in data.get("visible_panes") or []:
        if not isinstance(item, dict):
            continue
        item = _rename_snapshot_keys(item)
        pane_id = str(item.get("pane_id") or "").strip()
        if not pane_id:
            continue
        distance = item.get("distance") or ""
        direction = item.get("direction") or ""
        panes.append(
            {
                "pane_id": pane_id,
                "distance": distance if distance in _SNAPSHOT_DISTANCES else "",
                "direction": direction if direction in _SNAPSHOT_DIRECTIONS else "",
                "visible": item.get("visible", True) is not False,
            }
        )
    preview = data.get("preview")
    cleaned_preview: dict[str, Any] | None = None
    if isinstance(preview, dict):
        preview = _rename_snapshot_keys(preview)
        container = preview.get("container") or ""
        cleaned_preview = {
            "container": container if container in _SNAPSHOT_CONTAINERS else "",
            "in_world": bool(preview.get("in_world")),
            "interactive": preview.get("interactive", True) is not False,
            "arbitrary_origins": bool(preview.get("arbitrary_origins")),
            "video": bool(preview.get("video")),
            "keyboard": preview.get("keyboard", True) is not False,
        }
    phase = data.get("npc_phase") or ""
    substate = data.get("locomotion_substate") or ""
    try:
        revision = int(data.get("client_revision") or 1)
    except (TypeError, ValueError):
        revision = 1
    if revision < 1:
        revision = 1
    image_refs = data.get("image_refs") or []
    capabilities = data.get("capabilities") or []
    captures = data.get("captures") or []
    return {
        "client_revision": revision,
        "primary_pane_id": str(data.get("primary_pane_id") or ""),
        "focused_pane_ids": [
            str(p) for p in list(data.get("focused_pane_ids") or [])[:8] if str(p)
        ],
        "visible_panes": panes,
        "npc_phase": phase if phase in _SNAPSHOT_PHASES else "",
        "locomotion_substate": substate if substate in _SNAPSHOT_SUBSTATES else "",
        "capabilities": list(capabilities)[:16] if isinstance(capabilities, list) else [],
        "image_refs": list(image_refs) if isinstance(image_refs, list) else [],
        "visible_text": str(data.get("visible_text") or "")[:4000],
        "captures": list(captures)[:8] if isinstance(captures, list) else [],
        "preview": cleaned_preview,
    }


def reject_spatial_payload(payload: dict[str, Any]) -> None:
    """Raise ValueError if a client sneaks coordinates into a semantic payload."""
    stack: list[Any] = [payload]
    while stack:
        cur = stack.pop()
        if isinstance(cur, dict):
            for key, val in cur.items():
                if str(key).strip().lower() in _FORBIDDEN_SPATIAL:
                    raise ValueError(f"spatial field '{key}' is not allowed")
                stack.append(val)
        elif isinstance(cur, list):
            stack.extend(cur)


class FirecrawlScrapeBody(BaseModel):
    url: str
    formats: list[str] = Field(default_factory=list)
    only_main_content: bool = True
    workspace_id: str = ""


class FirecrawlCrawlBody(BaseModel):
    url: str
    workspace_id: str = ""
    limit: int = 10


# --- Plan B domain ---


class WorkspaceCreate(BaseModel):
    name: str = Field(default="Untitled", min_length=1, max_length=120)
    description: str = ""
    repo_full_name: str | None = None
    default_branch: str = "main"
    vektral_branch: str = ""
    org_id: str | None = None
    create_github_repo: bool = False
    github_repo_private: bool = True
    linear_project_id: str | None = None
    linear_team_id: str | None = None


class WorkspacePatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    description: str | None = None
    repo_full_name: str | None = None
    default_branch: str | None = None
    vektral_branch: str | None = None
    thumbnail_url: str | None = None
    org_id: str | None = None
    linear_project_id: str | None = None
    linear_team_id: str | None = None


class WorkspaceView(BaseModel):
    id: str
    name: str
    description: str = ""
    slug: str = ""
    repo_full_name: str | None = None
    default_branch: str = "main"
    vektral_branch: str = ""
    storage_prefix: str = ""
    org_id: str | None = None
    is_personal: bool = True
    thumbnail_url: str | None = None
    owner_uid: str = ""
    linear_project_id: str | None = None
    linear_team_id: str | None = None
    linear_project_name: str | None = None
    linear_project_url: str | None = None
    linear_team_name: str | None = None
    linear_organization_id: str | None = None
    linear_organization_name: str | None = None
    site_ready: bool = True
    site_status: str = "ready"
    site_error: str = ""
    site_commit_sha: str = ""
    created_at: str = ""
    updated_at: str = ""


class OrgCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    slug: str | None = Field(default=None, max_length=80)


class OrgView(BaseModel):
    id: str
    name: str
    slug: str = ""
    my_role: str = "owner"
    my_capabilities: list[str] = Field(default_factory=list)
    member_count: int = 1
    owner_uid: str = ""
    has_join_code: bool = False
    join_code: str = ""
    created_at: str = ""


class MemberView(BaseModel):
    uid: str
    email: str = ""
    display_name: str = ""
    avatar_url: str = ""
    role: str = "member"
    status: str = "active"
    joined_at: str = ""


class MemberPatch(BaseModel):
    role: str = Field(min_length=1, max_length=32)


class InviteCreate(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    role: str = "member"


class InviteView(BaseModel):
    id: str
    email: str
    role: str = "member"
    status: str = "pending"
    org_id: str = ""
    org_name: str = ""
    created_at: str = ""


class JoinByCode(BaseModel):
    code: str = Field(min_length=1, max_length=32)


class MutationResult(BaseModel):
    ok: bool = True
    message: str = ""
    id: str = ""
    join_code: str = ""


class GithubStatus(BaseModel):
    connected: bool = False
    github_login: str = ""
    connected_at: str = ""
    configured: bool = False
    needs_reauth: bool = False


class GithubConnectStart(BaseModel):
    url: str = ""
    state: str = ""
    message: str = ""


class GithubRepoView(BaseModel):
    full_name: str
    html_url: str = ""
    default_branch: str = "main"
    private: bool = False
    description: str = ""
    pushed_at: str = ""
    updated_at: str = ""
    language: str = ""
    stargazers_count: int = 0


class GithubRepoVerifyRequest(BaseModel):
    repos: list[str] = Field(default_factory=list, max_length=50)


class GithubRepoVerifyResult(BaseModel):
    full_name: str
    accessible: bool
    reason: str = ""


class LinearStatus(BaseModel):
    connected: bool = False
    linear_user_id: str = ""
    linear_name: str = ""
    linear_email: str = ""
    connected_at: str = ""
    configured: bool = False
    default_organization_id: str = ""
    default_team_id: str = ""


class LinearConnectStart(BaseModel):
    url: str = ""
    state: str = ""
    message: str = ""


class LinearOrganizationView(BaseModel):
    id: str
    name: str
    url_key: str = ""
    scope: str = "organization"


class LinearTeamView(BaseModel):
    id: str
    name: str
    key: str = ""
    description: str = ""
    private: bool = False
    organization_id: str = ""
    organization_name: str = ""


class LinearProjectTeamView(BaseModel):
    id: str = ""
    name: str = ""
    key: str = ""


class LinearProjectView(BaseModel):
    id: str
    name: str
    description: str = ""
    url: str = ""
    state: str = ""
    team_id: str = ""
    team_name: str = ""
    team_key: str = ""
    teams: list[LinearProjectTeamView] = Field(default_factory=list)


class LinearProjectCreateBody(BaseModel):
    name: str
    description: str = ""
    team_id: str = ""


class LinearDefaultsBody(BaseModel):
    organization_id: str = ""
    team_id: str = ""


def workspace_view(doc: dict[str, Any]) -> dict[str, Any]:
    """Normalize workspace docs for VR + Web (includes description for dashboard)."""
    from app.store import workspace_to_view

    return workspace_to_view(doc)


def sanitize_preview_surface_url(url: str) -> str:
    """Drop ticket-bearing or legacy path-proxy URLs from public pane views."""
    raw = str(url or "").strip()
    if not raw:
        return ""
    lower = raw.lower()
    if "__vektral_auth" in lower or "ticket=" in lower:
        return ""
    if "/proxy/" in raw or "{session}" in raw:
        return ""
    return raw


def pane_view(doc: dict[str, Any], preview_base_url: str = "") -> dict[str, Any]:
    route = doc.get("route") or "/"
    if not str(route).startswith("/"):
        route = f"/{route}"
    kind = str(doc.get("kind") or "preview").strip().lower()
    if kind not in {"chat", "blank", "preview"}:
        kind = "preview"
    reload_v = int(doc.get("reload_version") or 0)
    source_kind = str(doc.get("source_kind") or "")
    if kind == "blank":
        source_kind = ""
        route = ""
    elif kind == "chat":
        source_kind = ""
    elif not source_kind:
        source_kind = "workspace-preview"
    preview = ""
    launch_url = ""
    if kind in {"chat", "blank"}:
        preview = ""
        launch_url = ""
    elif source_kind == "external":
        preview = str(doc.get("external_url") or "")
        launch_url = ""
    else:
        # Isolated origin is issued via /launch. Never leak tickets or /proxy URLs.
        launch_url = sanitize_preview_surface_url(str(doc.get("launch_url") or ""))
        preview = sanitize_preview_surface_url(
            launch_url or str(doc.get("preview_url") or "")
        )
    surface_status = str(doc.get("preview_status") or "")
    return {
        "id": doc.get("id") or "",
        "workspace_id": doc.get("workspace_id") or "",
        "title": doc.get("title") or "",
        "kind": kind,
        "route": route,
        "preview_url": "" if kind in {"chat", "blank"} else preview,
        "launch_url": "" if kind in {"chat", "blank"} else launch_url,
        "source_kind": source_kind,
        "runtime_id": "" if kind in {"chat", "blank"} else str(doc.get("runtime_id") or ""),
        "renderer": "" if kind in {"chat", "blank"} else str(doc.get("renderer") or "webview"),
        "external_url": str(doc.get("external_url") or "") if source_kind == "external" else "",
        "preview_status": "" if kind in {"chat", "blank"} else surface_status,
        "reload_policy": "" if kind in {"chat", "blank"} else str(doc.get("reload_policy") or "remount"),
        "unsupported_reason": "" if kind in {"chat", "blank"} else str(doc.get("unsupported_reason") or ""),
        "layout_json": doc.get("layout_json") or "",
        "reload_version": reload_v,
        "created_at": doc.get("created_at") or "",
        "updated_at": doc.get("updated_at") or "",
    }


def message_view(doc: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": doc.get("id") or "",
        "pane_id": doc.get("pane_id") or "",
        "role": doc.get("role") or "user",
        "text": doc.get("text") or "",
        "job_id": doc.get("job_id") or "",
        "created_at": doc.get("created_at") or "",
        "owner_uid": doc.get("owner_uid") or "",
    }


def _parsed_result_json(raw: Any) -> Any:
    """Object when the job stored a JSON object, so clients can read result_json.action."""
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str) or not raw.strip():
        return raw or ""
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return raw
    return parsed if isinstance(parsed, dict) else raw


def job_view(doc: dict[str, Any]) -> dict[str, Any]:
    raw_plan = doc.get("embodiment_json") or ""
    if isinstance(raw_plan, dict):
        plan = raw_plan
    else:
        try:
            parsed = json.loads(raw_plan) if raw_plan else {}
        except (json.JSONDecodeError, TypeError):
            parsed = {}
        plan = parsed if isinstance(parsed, dict) else {}
    return {
        "id": doc.get("id") or "",
        "status": doc.get("status") or "queued",
        "command_type": doc.get("command_type") or "",
        "command_text": doc.get("command_text") or "",
        "workspace_id": doc.get("workspace_id") or "",
        "pane_id": doc.get("pane_id") or "",
        "route": doc.get("route") or "",
        "model": doc.get("model") or "",
        "branch": doc.get("branch") or "",
        "commit_sha": doc.get("commit_sha") or "",
        "base_commit_sha": doc.get("base_commit_sha") or "",
        "preview_url": doc.get("preview_url") or "",
        "reload_version": int(doc.get("reload_version") or 0),
        "coding_targets": doc.get("coding_targets") or {},
        "affected_pane_ids": doc.get("affected_pane_ids") or [],
        "mutation_error_code": doc.get("mutation_error_code") or "",
        "vision_status": doc.get("vision_status") or "",
        "captures": doc.get("captures") or [],
        "logs": doc.get("logs") or "",
        "result_json": _parsed_result_json(doc.get("result_json")),
        "embodiment_json": plan,
        "error": doc.get("error") or "",
        "created_at": doc.get("created_at") or "",
        "updated_at": doc.get("updated_at") or "",
        "owner_uid": doc.get("owner_uid") or "",
    }


def preview_view(doc: dict[str, Any]) -> dict[str, Any]:
    status = str(doc.get("status") or "stopped")
    if status == "running":
        status = "ready"
    if status == "failed":
        status = "error"
    runtime = doc.get("runtime") if isinstance(doc.get("runtime"), dict) else {}
    return {
        "id": doc.get("id") or doc.get("workspace_id") or "",
        "workspace_id": doc.get("workspace_id") or "",
        "status": status,
        "branch": doc.get("branch") or "",
        "session_id": doc.get("session_id") or "",
        "preview_origin": doc.get("preview_origin") or "",
        "runtime": runtime,
        "runtime_id": doc.get("runtime_id") or runtime.get("runtimeId") or "",
        "commit_sha": doc.get("commit_sha") or "",
        "logs": doc.get("logs") or "",
        "error": doc.get("error") or "",
        "error_code": doc.get("error_code") or runtime.get("errorCode") or "",
        "created_at": doc.get("created_at") or "",
        "updated_at": doc.get("updated_at") or "",
        "starter_seeded": bool(doc.get("starter_seeded")),
    }


def scrape_item_view(doc: dict[str, Any], preview_base_url: str = "") -> dict[str, Any]:
    slug = doc.get("slug") or "page"
    route = f"/research/{slug}.html"
    base = (preview_base_url or "").rstrip("/")
    return {
        "id": doc.get("id") or "",
        "workspace_id": doc.get("workspace_id") or "",
        "url": doc.get("url") or "",
        "slug": slug,
        "title": doc.get("title") or "",
        "excerpt": doc.get("excerpt") or "",
        "route": route,
        "page_url": f"{base}{route}" if base else "",
        "created_at": doc.get("created_at") or "",
    }
