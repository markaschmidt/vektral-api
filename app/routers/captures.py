"""Opaque per-pane capture upload/download. Never returns client URLs to models."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import Response

from app.deps import current_user
from app.firebase_auth import VerifiedUser
from app.services.captures import capture_view, create_capture, get_capture
from app.services.workspaces import require_workspace_access

router = APIRouter(tags=["captures"])


@router.post("/api/workspaces/{workspace_id}/captures")
async def api_upload_capture(
    workspace_id: str,
    user: Annotated[VerifiedUser, Depends(current_user)],
    pane_id: str = Form(...),
    context_revision: int = Form(0),
    visible_text: str = Form(""),
    file: UploadFile = File(...),
) -> dict[str, Any]:
    require_workspace_access(workspace_id, user.uid)
    body = await file.read()
    ctype = file.content_type or "image/png"
    return create_capture(
        uid=user.uid,
        workspace_id=workspace_id,
        pane_id=pane_id,
        context_revision=context_revision,
        content_type=ctype,
        body=body,
        visible_text=visible_text,
    )


@router.get("/api/workspaces/{workspace_id}/captures/{capture_id}")
async def api_get_capture_meta(
    workspace_id: str,
    capture_id: str,
    user: Annotated[VerifiedUser, Depends(current_user)],
) -> dict[str, Any]:
    require_workspace_access(workspace_id, user.uid)
    doc = get_capture(capture_id, user.uid, workspace_id)
    if not doc:
        raise HTTPException(status_code=404, detail={"error": "not_found", "message": "capture not found"})
    return capture_view(doc)


@router.get("/api/workspaces/{workspace_id}/captures/{capture_id}/bytes")
async def api_get_capture_bytes(
    workspace_id: str,
    capture_id: str,
    user: Annotated[VerifiedUser, Depends(current_user)],
) -> Response:
    require_workspace_access(workspace_id, user.uid)
    doc = get_capture(capture_id, user.uid, workspace_id)
    if not doc:
        raise HTTPException(status_code=404, detail={"error": "not_found", "message": "capture not found"})
    raw = doc.get("bytes")
    if not isinstance(raw, (bytes, bytearray)):
        import base64

        raw = base64.b64decode(str(doc.get("body_b64") or ""))
    return Response(
        content=bytes(raw),
        media_type=str(doc.get("content_type") or "image/png"),
        headers={"Cache-Control": "private, max-age=60"},
    )
