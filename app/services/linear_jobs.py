"""Linear issue (card) jobs — Vocal Bridge → Mistral JSON plan → GraphQL."""

from __future__ import annotations

import json
import logging
from typing import Any

from fastapi import HTTPException

from app.agent_kv import now_iso, put_doc
from app.services import linear_client as linear_svc
from app.services import panes as panes_svc
from app.services.coding import (
    _default_llm_model,
    _llm_chat,
    _llm_provider_label,
    extract_edit_plan,
)
from app.store import get_store

logger = logging.getLogger("vektral.linear_jobs")

LINEAR_STRONG = [
    "linear",
    "ticket",
    "tickets",
    "backlog",
    "kanban",
    "sprint card",
    "sprint cards",
]

LINEAR_ISSUE_PHRASES = [
    "create an issue",
    "create a issue",
    "create issues",
    "new issue",
    "new ticket",
    "update the issue",
    "update issue",
    "close the issue",
    "close issue",
    "archive the issue",
    "archive issue",
    "delete the ticket",
    "delete ticket",
    "assign the issue",
    "assign issue",
    "move the issue",
    "move issue",
    "linear issue",
    "linear ticket",
]

_SYSTEM_PROMPT = (
    "You are Vektral's Linear agent. The user spoke a command about Linear "
    "issues (cards). Respond ONLY with a single JSON object (no markdown "
    "commentary) shaped as:\n"
    "{\n"
    '  "summary": "short description",\n'
    '  "actions": [\n'
    '    {"op": "create", "title": "...", "description": "...", "priority": 2, '
    '"team_id": ""},\n'
    '    {"op": "update", "id": "", "identifier": "ABC-12", "title": "", '
    '"description": "", "state": "Done", "priority": 2},\n'
    '    {"op": "archive", "id": "", "identifier": "ABC-12"}\n'
    "  ],\n"
    '  "done": true\n'
    "}\n"
    "Rules:\n"
    "- Use identifier (TEAM-123) when the user names a ticket.\n"
    "- Prefer existing issue ids from the context list when referring by title.\n"
    "- archive covers delete / close / remove requests.\n"
    "- You may return multiple actions for a set of cards.\n"
    "- Omit fields you are not changing.\n"
    "- Default team_id is provided in context; use it for creates if unspecified.\n"
)


def is_linear_command(text: str) -> bool:
    lowered = (text or "").lower()
    if not lowered:
        return False
    for word in LINEAR_STRONG:
        if word in lowered:
            return True
    for phrase in LINEAR_ISSUE_PHRASES:
        if phrase in lowered:
            return True
    return False


def _append_log(job: dict[str, Any], line: str) -> None:
    prev = (job.get("logs") or "").rstrip()
    job["logs"] = f"{prev}\n{line}".strip() if prev else line


def _fail(job: dict[str, Any], error: str, extra_log: str = "") -> dict[str, Any]:
    job["status"] = "failed"
    job["error"] = error
    if extra_log:
        _append_log(job, extra_log)
    else:
        _append_log(job, error)
    job["updated_at"] = now_iso()
    put_doc("jobs", job["id"], job)
    return job


def _save(job: dict[str, Any], status: str) -> None:
    job["status"] = status
    job["updated_at"] = now_iso()
    put_doc("jobs", job["id"], job)


def _priority(raw: Any) -> int | None:
    if raw is None or raw == "":
        return None
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return None
    if value < 0 or value > 4:
        return None
    return value


async def _execute_action(
    token: str,
    action: dict[str, Any],
    *,
    default_team_id: str,
    viewer_id: str,
) -> dict[str, Any]:
    op = str(action.get("op") or action.get("action") or "").strip().lower()
    if op in {"delete", "close", "remove"}:
        op = "archive"
    if op not in {"create", "update", "archive"}:
        return {"ok": False, "op": op or "unknown", "error": f"unsupported op {op}"}

    try:
        if op == "create":
            team_id = str(action.get("team_id") or default_team_id or "").strip()
            title = str(action.get("title") or "").strip()
            if not title:
                return {"ok": False, "op": op, "error": "create requires title"}
            if not team_id:
                return {
                    "ok": False,
                    "op": op,
                    "error": "no Linear team selected — pick a default team under Integrations",
                }
            state_name = str(action.get("state") or "").strip()
            state_id = ""
            if state_name:
                state_id = await linear_svc.resolve_state_id(token, team_id, state_name)
            assignee = str(action.get("assignee") or "").strip()
            assignee_id = viewer_id if assignee.lower() in {"me", "myself", "self"} else assignee
            issue = await linear_svc.create_issue(
                token,
                team_id=team_id,
                title=title,
                description=str(action.get("description") or ""),
                priority=_priority(action.get("priority")),
                assignee_id=assignee_id,
                state_id=state_id,
            )
            return {"ok": True, "op": op, "issue": issue}

        found = await linear_svc.resolve_issue(
            token,
            issue_id=str(action.get("id") or ""),
            identifier=str(action.get("identifier") or ""),
            title=str(action.get("title") or ""),
        )
        issue_id = str(found.get("id") or "").strip()
        if not issue_id:
            return {
                "ok": False,
                "op": op,
                "error": "could not resolve Linear issue",
            }

        if op == "archive":
            ok = await linear_svc.archive_issue(token, issue_id)
            return {
                "ok": ok,
                "op": op,
                "issue": found,
                "error": "" if ok else "archive failed",
            }

        team_id = str(found.get("team_id") or default_team_id or "")
        state_name = str(action.get("state") or "").strip()
        state_id = ""
        if state_name and team_id:
            state_id = await linear_svc.resolve_state_id(token, team_id, state_name)
        assignee = str(action.get("assignee") or "").strip()
        assignee_id = ""
        if assignee:
            assignee_id = (
                viewer_id if assignee.lower() in {"me", "myself", "self"} else assignee
            )
        description = action.get("description")
        desc_arg: str | None
        if description is None:
            desc_arg = None
        else:
            desc_arg = str(description)
        updated = await linear_svc.update_issue(
            token,
            issue_id,
            title=str(action.get("title") or ""),
            description=desc_arg,
            priority=_priority(action.get("priority")),
            assignee_id=assignee_id,
            state_id=state_id,
        )
        return {"ok": True, "op": op, "issue": updated}
    except HTTPException as exc:
        detail = exc.detail
        if isinstance(detail, dict):
            message = str(detail.get("message") or detail)
        else:
            message = str(detail)
        return {"ok": False, "op": op, "error": message[:400]}
    except Exception as exc:  # noqa: BLE001
        logger.exception("Linear action failed")
        return {"ok": False, "op": op, "error": str(exc)[:400]}


async def run_linear_job(job: dict[str, Any], uid: str) -> dict[str, Any]:
    store = get_store()
    status = store.linear_status(uid)
    if not status.get("connected"):
        return _fail(job, "connect Linear first (Integrations → Linear)")

    token = store.linear_access_token(uid)
    if not token:
        return _fail(job, "connect Linear first (Integrations → Linear)")

    default_team_id = str(status.get("default_team_id") or "")
    default_org_id = str(status.get("default_organization_id") or "")

    provider = _llm_provider_label()
    _save(job, "planning")
    _append_log(job, f"Calling {provider} for Linear card actions…")
    put_doc("jobs", job["id"], job)

    context_bits = [
        f"Default organization_id: {default_org_id or '(personal)'}",
        f"Default team_id: {default_team_id or '(none — user must pick a team for creates)'}",
        f"Linear user: {status.get('linear_name') or ''} {status.get('linear_email') or ''}".strip(),
    ]
    try:
        recent = await linear_svc.recent_issues(token, team_id=default_team_id, first=20)
        if recent:
            lines = [
                f"- {i.get('identifier')} {i.get('title')} [{i.get('state')}] id={i.get('id')}"
                for i in recent
            ]
            context_bits.append("Open issues:\n" + "\n".join(lines))
    except HTTPException as exc:
        logger.warning("Linear context fetch failed: %s", exc.detail)
        context_bits.append("Open issues: (unavailable)")

    llm = await _llm_chat(
        model=str(job.get("model") or _default_llm_model()),
        system=_SYSTEM_PROMPT,
        user=(
            "Context:\n"
            + "\n".join(context_bits)
            + "\n\nUser command:\n"
            + str(job.get("command_text") or "")
        ),
        history=panes_svc.history_for_llm(
            str(job.get("chat_pane_id") or ""),
            exclude_text=str(job.get("command_text") or ""),
        ),
    )
    if not bool(llm.get("ok")):
        return _fail(job, str(llm.get("error") or f"{provider} call failed"))

    content = str(llm.get("content") or "")
    plan = extract_edit_plan(content)
    if plan is None:
        return _fail(
            job,
            "model did not return a valid Linear action plan",
            extra_log=content[:1000],
        )

    raw_actions = plan.get("actions") or []
    if not isinstance(raw_actions, list) or not raw_actions:
        return _fail(
            job,
            "model returned no Linear actions",
            extra_log=str(plan.get("summary") or "")[:500],
        )

    _save(job, "updating")
    _append_log(job, f"Executing {len(raw_actions)} Linear action(s)…")
    put_doc("jobs", job["id"], job)

    viewer_id = str(status.get("linear_user_id") or "")
    results: list[dict[str, Any]] = []
    for action in raw_actions:
        if not isinstance(action, dict):
            results.append({"ok": False, "op": "unknown", "error": "invalid action"})
            continue
        result = await _execute_action(
            token,
            action,
            default_team_id=default_team_id,
            viewer_id=viewer_id,
        )
        results.append(result)
        ident = ((result.get("issue") or {}).get("identifier") if isinstance(result.get("issue"), dict) else "") or ""
        if result.get("ok"):
            _append_log(job, f"{result.get('op')} ok {ident}".strip())
        else:
            _append_log(job, f"{result.get('op')} failed: {result.get('error')}")

    ok_count = sum(1 for r in results if r.get("ok"))
    summary = str(plan.get("summary") or "").strip() or f"{ok_count}/{len(results)} Linear actions"
    job["result_json"] = json.dumps(
        {
            "summary": summary,
            "actions": results,
        }
    )
    if ok_count == 0:
        return _fail(job, summary or "all Linear actions failed")

    job["error"] = ""
    _append_log(job, "Linear job ready.")
    _save(job, "ready")
    return job
