"""Mistral/OpenRouter coding agent — parse file edits and apply via Collab git APIs.

Expected model reply (JSON object, optional markdown fence)::

    {
      "summary": "short description",
      "files": [{"path": "src/App.tsx", "content": "...full file..."}],
      "commit_message": "concise commit message",
      "done": true
    }

Pipeline on success: ``/v1/git/write_files`` → ``/v1/git/commit_push`` →
preview restart → bump pane ``reload_version`` → job ``ready`` with
``preview_url`` / ``commit_sha``.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

import httpx

from app.agent_kv import get_doc, now_iso, put_doc
from app.config import get_settings
from app.services import panes as panes_svc
from app.services import preview as preview_svc
from app.services.preview_client import git_file, git_tree, preview_runner_request
from app.services.workspaces import (
    branch_name,
    get_workspace,
    github_token_for_user,
)

logger = logging.getLogger("vektral.coding")

_FENCE_RE = re.compile(r"^```(?:json|JSON)?\s*\n([\s\S]*?)\n```\s*$", re.MULTILINE)


def extract_edit_plan(text: str) -> dict[str, Any] | None:
    """Parse a JSON edit plan from raw model text (fenced or bare)."""
    if not text or not str(text).strip():
        return None
    cleaned = str(text).strip()
    if cleaned.startswith("```"):
        m = _FENCE_RE.match(cleaned)
        if m:
            cleaned = m.group(1).strip()
        else:
            lines = cleaned.split("\n")
            if len(lines) >= 2:
                if lines[-1].strip().startswith("```"):
                    cleaned = "\n".join(lines[1:-1]).strip()
                else:
                    cleaned = "\n".join(lines[1:]).strip()
    try:
        parsed = json.loads(cleaned)
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        pass
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start >= 0 and end > start:
        try:
            parsed2 = json.loads(cleaned[start : end + 1])
            if isinstance(parsed2, dict):
                return parsed2
        except json.JSONDecodeError:
            return None
    return None


def files_from_plan(plan: dict[str, Any]) -> list[dict[str, str]]:
    """Normalize ``plan["files"]`` into ``[{path, content}, ...]``."""
    raw = plan.get("files") or []
    out: list[dict[str, str]] = []
    if not isinstance(raw, list):
        return out
    for item in raw:
        if not isinstance(item, dict):
            continue
        path = str(item.get("path") or "").strip().lstrip("/")
        if not path or ".." in path.split("/"):
            continue
        if item.get("content") is None:
            continue
        out.append({"path": path, "content": str(item["content"])})
    return out


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


async def _openrouter_chat(
    *,
    model: str,
    system: str,
    user: str,
    history: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    s = get_settings()
    headers = {
        "Authorization": f"Bearer {s.openrouter_api_key}",
        "Content-Type": "application/json",
        "HTTP-Referer": s.openrouter_http_referer or "https://vektral.local",
        "X-Title": s.openrouter_x_title or "Vektral",
    }
    messages: list[dict[str, str]] = [{"role": "system", "content": system}]
    for item in history or []:
        role = str(item.get("role") or "user")
        if role not in ("user", "assistant"):
            role = "user"
        content = str(item.get("content") or "")
        if content:
            messages.append({"role": role, "content": content})
    messages.append({"role": "user", "content": user})
    body = {
        "model": model or s.openrouter_model,
        "messages": messages,
    }
    try:
        async with httpx.AsyncClient(timeout=120.0) as client:
            resp = await client.post(
                f"{s.openrouter_base_url.rstrip('/')}/chat/completions",
                headers=headers,
                json=body,
            )
            data = resp.json() if resp.content else {}
            if resp.status_code >= 400:
                return {
                    "ok": False,
                    "error": f"OpenRouter HTTP {resp.status_code}: {str(data)[:400]}",
                }
            content = ""
            choices = data.get("choices") or []
            if choices:
                content = str((choices[0].get("message") or {}).get("content") or "")
            return {"ok": True, "content": content, "raw": data}
    except Exception as exc:  # noqa: BLE001
        logger.exception("openrouter call failed")
        return {"ok": False, "error": str(exc)}


_SYSTEM_PROMPT = (
    "You are Vektral's coding agent. Edit a React/Vite/TypeScript web app. "
    "Respond ONLY with a single JSON object (no markdown commentary) shaped as:\n"
    "{\n"
    '  "summary": "short description of changes",\n'
    '  "files": [{"path": "relative/path.tsx", "content": "full file contents"}],\n'
    '  "commit_message": "concise commit message",\n'
    '  "done": true\n'
    "}\n"
    "Rules:\n"
    "- Return FULL file contents for each edited file.\n"
    "- Prefer minimal edits that satisfy the user request.\n"
    "- Keep TypeScript/React/Tailwind idioms.\n"
)


async def _mistral_chat(
    *,
    model: str,
    system: str,
    user: str,
    history: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    s = get_settings()
    headers = {
        "Authorization": f"Bearer {s.mistral_api_key}",
        "Content-Type": "application/json",
    }
    messages: list[dict[str, str]] = [{"role": "system", "content": system}]
    for item in history or []:
        role = str(item.get("role") or "user")
        if role not in ("user", "assistant"):
            role = "user"
        content = str(item.get("content") or "")
        if content:
            messages.append({"role": role, "content": content})
    messages.append({"role": "user", "content": user})
    body = {
        "model": model or s.mistral_model,
        "messages": messages,
    }
    try:
        async with httpx.AsyncClient(timeout=120.0) as client:
            resp = await client.post(
                f"{s.mistral_base_url}/chat/completions",
                headers=headers,
                json=body,
            )
            data = resp.json() if resp.content else {}
            if resp.status_code >= 400:
                return {
                    "ok": False,
                    "error": f"Mistral HTTP {resp.status_code}: {str(data)[:400]}",
                }
            content = ""
            choices = data.get("choices") or []
            if choices:
                content = str((choices[0].get("message") or {}).get("content") or "")
            return {"ok": True, "content": content, "raw": data}
    except Exception as exc:  # noqa: BLE001
        logger.exception("mistral call failed")
        return {"ok": False, "error": str(exc)}


async def _llm_chat(
    *,
    model: str,
    system: str,
    user: str,
    history: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    """Mistral when configured; otherwise OpenRouter."""
    s = get_settings()
    if s.mistral_api_key:
        return await _mistral_chat(model=model, system=system, user=user, history=history)
    if s.openrouter_api_key:
        return await _openrouter_chat(model=model, system=system, user=user, history=history)
    return {
        "ok": False,
        "error": (
            "No LLM configured — set OPENROUTER_API_KEY or MISTRAL_API_KEY on Vektral-API"
        ),
    }


def _llm_provider_label() -> str:
    s = get_settings()
    if s.mistral_api_key:
        return "Mistral"
    if s.openrouter_api_key:
        return "OpenRouter"
    return "LLM"


def _default_llm_model() -> str:
    s = get_settings()
    if s.mistral_api_key:
        return s.mistral_model
    if s.openrouter_api_key:
        return s.openrouter_model
    return s.mistral_model


_ENTRY_FILES = (
    "src/App.tsx",
    "src/App.jsx",
    "src/main.tsx",
    "src/main.jsx",
    "src/pages/index.tsx",
    "index.html",
    "App.tsx",
)
_MAX_FILES = 4
_MAX_FILE_CHARS = 8000
_MAX_TOTAL_CHARS = 24000


def _route_dirs(route: str) -> list[str]:
    r = (route or "/").strip() or "/"
    if not r.startswith("/"):
        r = f"/{r}"
    if r.startswith("/chat"):
        r = "/"
    parts = [p for p in r.strip("/").split("/") if p and "." not in p]
    dirs = ["", "src", "src/pages", "src/app"]
    if parts:
        dirs.extend(
            [
                "/".join(parts),
                "src/" + "/".join(parts),
                "src/pages/" + "/".join(parts),
                "src/app/" + "/".join(parts),
            ]
        )
    # unique preserve order
    seen: set[str] = set()
    out: list[str] = []
    for d in dirs:
        if d not in seen:
            seen.add(d)
            out.append(d)
    return out


def _utterance_paths(text: str) -> list[str]:
    hits: list[str] = []
    for token in re.findall(r"[A-Za-z0-9_./-]+\.(?:tsx|ts|jsx|js|css|html|json)", text or ""):
        cleaned = token.lstrip("./")
        if cleaned and ".." not in cleaned.split("/"):
            hits.append(cleaned)
    return hits


async def gather_repo_context(
    workspace_id: str,
    route: str,
    utterance: str = "",
) -> tuple[str, list[str]]:
    """Read a few route-relevant files from the Collab checkout. Empty on miss."""
    wanted: list[str] = []
    wanted.extend(_utterance_paths(utterance))
    stem = [p for p in (route or "/").strip("/").split("/") if p]
    if stem:
        last = stem[-1]
        wanted.extend(
            [
                f"src/{last}.tsx",
                f"src/pages/{last}.tsx",
                f"src/pages/{last}/index.tsx",
                f"{last}.html",
            ]
        )
    wanted.extend(_ENTRY_FILES)

    tree_names: set[str] = set()
    for directory in _route_dirs(route)[:6]:
        tree = await git_tree(workspace_id, directory)
        if not bool(tree.get("ok")):
            continue
        for entry in tree.get("entries") or []:
            if not isinstance(entry, dict):
                continue
            if entry.get("type") != "file":
                continue
            path = str(entry.get("path") or "")
            if path:
                tree_names.add(path)
                name = str(entry.get("name") or "")
                if stem and name.lower().startswith(stem[-1].lower()):
                    wanted.insert(0, path)

    if not tree_names and not wanted:
        return "", []

    picked: list[str] = []
    seen: set[str] = set()
    for path in wanted:
        if path in seen:
            continue
        seen.add(path)
        picked.append(path)
        if len(picked) >= _MAX_FILES:
            break
    if not picked and tree_names:
        picked = list(sorted(tree_names))[:_MAX_FILES]

    chunks: list[str] = []
    used: list[str] = []
    total = 0
    for path in picked:
        resp = await git_file(workspace_id, path)
        if not bool(resp.get("ok")):
            continue
        content = str(resp.get("content") or "")
        if not content:
            continue
        if len(content) > _MAX_FILE_CHARS:
            content = content[:_MAX_FILE_CHARS] + "\n/* …truncated */\n"
        piece = f"--- {path} ---\n{content}"
        if total + len(piece) > _MAX_TOTAL_CHARS:
            remain = _MAX_TOTAL_CHARS - total
            if remain < 200:
                break
            piece = piece[:remain] + "\n/* …truncated */\n"
        chunks.append(piece)
        used.append(path)
        total += len(piece)
        if len(used) >= _MAX_FILES:
            break
    if not chunks:
        return "", []
    return "Repo files:\n" + "\n\n".join(chunks), used


async def run_coding_job(job: dict[str, Any], uid: str) -> dict[str, Any]:
    """Run LLM (Mistral or OpenRouter) → Collab write/commit/restart → ready."""
    s = get_settings()
    if not s.mistral_api_key and not s.openrouter_api_key:
        return _fail(
            job,
            "No LLM configured — set OPENROUTER_API_KEY or MISTRAL_API_KEY on Vektral-API",
        )

    workspace_id = str(job.get("workspace_id") or "")
    if not workspace_id:
        return _fail(job, "workspace_id required for coding jobs")

    ws = get_workspace(workspace_id)
    if ws is None:
        return _fail(job, "workspace not found")
    if not ws.get("repo_full_name"):
        return _fail(job, "workspace has no GitHub repo")

    token = github_token_for_user(uid) or s.github_token_fallback
    if not token:
        return _fail(job, "connect GitHub with repo scope first (or set GITHUB_TOKEN)")

    branch = (
        str(job.get("branch") or "")
        or str(ws.get("vektral_branch") or "")
        or branch_name(workspace_id)
    )
    job["branch"] = branch
    route = panes_svc.preview_route_for_coding(
        workspace_id,
        str(job.get("route") or "/") or "/",
        str(job.get("pane_id") or ""),
    )
    job["route"] = route

    provider = _llm_provider_label()
    _save(job, "planning")
    _append_log(job, f"Calling {provider} for edits (route {route})…")
    put_doc("jobs", job["id"], job)

    repo_blob, repo_paths = await gather_repo_context(
        workspace_id,
        route,
        str(job.get("command_text") or ""),
    )
    if repo_paths:
        _append_log(job, f"Repo context: {', '.join(repo_paths)}")
        put_doc("jobs", job["id"], job)
    elif repo_blob == "":
        _append_log(job, "Repo context skipped (checkout missing or empty).")
        put_doc("jobs", job["id"], job)

    history = panes_svc.history_for_llm(
        str(job.get("chat_pane_id") or ""),
        exclude_text=str(job.get("command_text") or ""),
    )
    user_prompt = str(job.get("command_text") or "")
    if repo_blob:
        user_prompt = f"{repo_blob}\n\nUser command:\n{user_prompt}"

    llm = await _llm_chat(
        model=str(job.get("model") or _default_llm_model()),
        system=_SYSTEM_PROMPT + f"- Focus on pane route: {route}\n",
        user=user_prompt,
        history=history,
    )
    if not bool(llm.get("ok")):
        return _fail(job, str(llm.get("error") or f"{provider} call failed"))

    content = str(llm.get("content") or "")
    plan = extract_edit_plan(content)
    if plan is None:
        return _fail(
            job,
            "model did not return valid JSON edit plan",
            extra_log=content[:1000],
        )

    write_list = files_from_plan(plan)
    if not write_list:
        return _fail(
            job,
            "model returned no file edits",
            extra_log=str(plan.get("summary") or "")[:500],
        )

    _save(job, "editing")
    _append_log(job, f"Writing {len(write_list)} file(s) via Collab…")
    put_doc("jobs", job["id"], job)

    write_resp = await preview_runner_request(
        "POST",
        "/v1/git/write_files",
        {"workspace_id": workspace_id, "files": write_list},
    )
    if not bool(write_resp.get("ok")):
        return _fail(
            job,
            str(write_resp.get("error") or "write_files failed"),
            extra_log=str(write_resp)[:800],
        )
    written = write_resp.get("written") or [f["path"] for f in write_list]
    _append_log(job, f"Wrote: {', '.join(str(p) for p in written)}")

    _save(job, "committing")
    commit_msg = str(plan.get("commit_message") or "").strip() or "vektral agent edit"
    summary = str(plan.get("summary") or "").strip() or commit_msg
    put_doc("jobs", job["id"], job)

    push_resp = await preview_runner_request(
        "POST",
        "/v1/git/commit_push",
        {
            "workspace_id": workspace_id,
            "branch": branch,
            "message": commit_msg,
            "github_token": token,
            "repo_full_name": ws.get("repo_full_name") or "",
        },
    )
    if not bool(push_resp.get("ok")):
        return _fail(
            job,
            str(push_resp.get("error") or "commit_push failed"),
            extra_log=str(push_resp.get("logs") or "")[-2000:],
        )

    commit_sha = str(push_resp.get("commit_sha") or "")
    job["commit_sha"] = commit_sha
    _append_log(job, f"Committed {commit_sha}: {summary}")

    _save(job, "restarting_preview")
    _append_log(job, "Restarting preview…")
    put_doc("jobs", job["id"], job)

    prev = await preview_svc.restart_preview(workspace_id, uid)
    if str(prev.get("status") or "") == "failed":
        return _fail(
            job,
            str(prev.get("error") or "preview restart failed"),
        )

    preview_base = str(prev.get("preview_base_url") or "")
    if not preview_base:
        sess = get_doc("preview_sessions", workspace_id) or {}
        preview_base = str(sess.get("preview_base_url") or "")

    pane_id = str(job.get("pane_id") or "")
    reload_v = int(job.get("reload_version") or 0)
    preview_url = ""
    target = panes_svc.find_pane(pane_id) if pane_id else None
    if target is not None and not panes_svc.is_chat_pane(target):
        bumped = panes_svc.bump_pane_reload(pane_id)
        if bumped:
            reload_v = int(bumped.get("reload_version") or reload_v)
            preview_url = str(bumped.get("preview_url") or "")
    else:
        bumped_list = panes_svc.bump_workspace_panes(workspace_id)
        if bumped_list:
            reload_v = int(bumped_list[0].get("reload_version") or reload_v)
            for p in bumped_list:
                if p.get("route") == route:
                    preview_url = str(p.get("preview_url") or "")
                    reload_v = int(p.get("reload_version") or reload_v)
                    break
            if not preview_url:
                preview_url = str(bumped_list[0].get("preview_url") or "")

    if not preview_url and preview_base:
        r = route if route.startswith("/") else f"/{route}"
        preview_url = f"{preview_base.rstrip('/')}{r}?v={reload_v}"

    job["preview_url"] = preview_url
    job["reload_version"] = reload_v
    job["error"] = ""
    job["result_json"] = json.dumps(
        {
            "summary": summary,
            "files": [f["path"] for f in write_list],
            "written": list(written) if isinstance(written, list) else [],
            "commit_sha": commit_sha,
            "branch": branch,
            "preview_url": preview_url,
            "commit_message": commit_msg,
        }
    )
    _append_log(job, "Job ready — panes should reload.")
    _save(job, "ready")
    return job
