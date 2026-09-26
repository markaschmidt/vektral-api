"""Immutable multi-pane coding targets resolved once at job submission."""

from __future__ import annotations

import logging
from typing import Any

from fastapi import HTTPException

from app.agent_kv import get_doc
from app.services import embodiment as emb_svc
from app.services import panes as panes_svc
from app.services.preview_client import preview_runner_request

logger = logging.getLogger("vektral.coding_targets")

MAX_TARGETS = 8


class NoCodingTargets(Exception):
    error_code = "NO_CODING_TARGETS"


def _metric(event: str, **fields: Any) -> None:
    logger.info("preview_metric event=%s %s", event, fields)


def _preview_surface(pane: dict[str, Any] | None) -> bool:
    if not pane or panes_svc.is_chat_pane(pane):
        return False
    if str(pane.get("source_kind") or "workspace-preview") == "external":
        return False
    return True


def _default_preview_pane(workspace_id: str) -> dict[str, Any] | None:
    sess = get_doc("preview_sessions", workspace_id) or {}
    pid = str(sess.get("default_preview_pane_id") or "")
    pane = panes_svc.find_pane(pid) if pid else None
    if pane and pane.get("workspace_id") == workspace_id and _preview_surface(pane):
        return pane
    return None


def snapshot_from_panes(
    panes: list[dict[str, Any]],
    *,
    primary_pane_id: str,
    context_revision: int,
    base_commit_sha: str,
    runtime_id: str,
) -> dict[str, Any]:
    targets: list[dict[str, Any]] = []
    seen: set[str] = set()
    ordered = list(panes)
    if primary_pane_id:
        ordered.sort(key=lambda p: 0 if p.get("id") == primary_pane_id else 1)
    for pane in ordered:
        pid = str(pane.get("id") or "")
        if not pid or pid in seen:
            continue
        seen.add(pid)
        route = str(pane.get("route") or "/")
        if not route.startswith("/"):
            route = f"/{route}"
        targets.append(
            {
                "pane_id": pid,
                "runtime_id": str(pane.get("runtime_id") or runtime_id),
                "route": route,
                "title": str(pane.get("title") or pid),
            }
        )
        if len(targets) >= MAX_TARGETS:
            break
    primary = primary_pane_id if any(t["pane_id"] == primary_pane_id for t in targets) else (
        targets[0]["pane_id"] if targets else ""
    )
    return {
        "context_revision": int(context_revision or 0),
        "base_commit_sha": base_commit_sha,
        "primary_target_pane_id": primary,
        "targets": targets,
    }


async def fetch_checkout_head(workspace_id: str) -> dict[str, Any]:
    return await preview_runner_request(
        "GET", f"/v1/git/{workspace_id}/head", None, timeout=20.0
    )


async def resolve_coding_targets(
    workspace_id: str,
    uid: str,
    command: dict[str, Any],
) -> dict[str, Any]:
    """Pure authorized resolver. Never silently substitutes Home `/`."""
    cmd_ids = [str(p) for p in (command.get("focused_pane_ids") or []) if str(p)]
    cmd_rev = int(command.get("context_revision") or 0)
    primary = str(command.get("primary_pane_id") or "")
    ctx = emb_svc.get_context(workspace_id, uid)

    ids: list[str] = []
    revision = cmd_rev
    if cmd_ids:
        ids = cmd_ids
        if cmd_rev and ctx and int(ctx.get("client_revision") or 0) not in {0, cmd_rev}:
            # Command-supplied ids still win; stored context is not mutated onto the job.
            _metric("coding_targets_command_revision", workspace_id=workspace_id, cmd_rev=cmd_rev)
    elif ctx:
        if cmd_rev and int(ctx.get("client_revision") or 0) != cmd_rev:
            raise HTTPException(
                status_code=409,
                detail={"error": "context_revision_mismatch", "message": "stored context is not that revision"},
            )
        ids = [str(p) for p in (ctx.get("focused_pane_ids") or []) if str(p)]
        primary = primary or str(ctx.get("primary_pane_id") or "")
        revision = int(ctx.get("client_revision") or cmd_rev or 0)

    # Chat/command pane_id is conversation identity, not an automatic visual target.
    panes: list[dict[str, Any]] = []
    for pid in ids:
        pane = panes_svc.find_pane(pid)
        if pane is None or pane.get("workspace_id") != workspace_id:
            raise HTTPException(
                status_code=400,
                detail={"error": "unknown_pane", "message": f"pane {pid} is not in this workspace"},
            )
        if not _preview_surface(pane):
            continue
        panes.append(pane)

    if not panes:
        fallback = _default_preview_pane(workspace_id)
        if fallback is None:
            _metric("no_coding_targets", workspace_id=workspace_id)
            raise HTTPException(
                status_code=400,
                detail={
                    "error": "NO_CODING_TARGETS",
                    "message": "No workspace-preview panes in the focused set, and no default preview pane is configured",
                },
            )
        panes = [fallback]
        primary = str(fallback.get("id") or "")

    if primary and not any(p.get("id") == primary for p in panes):
        primary = str(panes[0].get("id") or "")
    elif not primary:
        primary = str(panes[0].get("id") or "")

    sess = get_doc("preview_sessions", workspace_id) or {}
    runtime_id = str(sess.get("runtime_id") or panes[0].get("runtime_id") or "")
    head = await fetch_checkout_head(workspace_id)
    sha = str(head.get("commit_sha") or sess.get("commit_sha") or "")
    snap = snapshot_from_panes(
        panes,
        primary_pane_id=primary,
        context_revision=revision,
        base_commit_sha=sha,
        runtime_id=runtime_id,
    )
    _metric(
        "coding_targets_resolved",
        workspace_id=workspace_id,
        count=len(snap["targets"]),
        revision=revision,
    )
    return snap
