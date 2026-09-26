"""Mistral/OpenRouter coding agent — parse file edits and apply via Collab.

Expected model reply (JSON object, optional markdown fence)::

    {
      "summary": "short description",
      "files": [{"path": "src/App.tsx", "content": "...full file...", "pane_ids": []}],
      "commit_message": "concise commit message",
      "done": true
    }

Pipeline: gather locators → LLM → POST /v1/workspaces/{id}/mutations →
selective remount (or restart on config/lockfile) → bump affected panes only.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any

import httpx

from app.agent_kv import now_iso, put_doc
from app.config import get_settings
from app.services import panes as panes_svc
from app.services.preview_client import git_file, git_tree
from app.services.workspaces import (
    branch_name,
    get_workspace,
    github_token_for_user,
    is_site_ready,
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


def _user_content(user: str, images: list[str] | None = None) -> Any:
    """Attach only server-built data URLs. Never fetch arbitrary client URLs."""
    refs: list[str] = []
    for raw in images or []:
        url = str(raw).strip()
        if not url.lower().startswith("data:image/"):
            continue
        header = url.split(",", 1)[0].lower()
        if not any(t in header for t in ("image/png", "image/jpeg", "image/jpg", "image/webp")):
            continue
        refs.append(url)
        if len(refs) >= 4:
            break
    if not refs:
        return user
    parts: list[dict[str, Any]] = [{"type": "text", "text": user}]
    for url in refs:
        parts.append({"type": "image_url", "image_url": {"url": url}})
    return parts


async def _openrouter_chat(
    *,
    model: str,
    system: str,
    user: str,
    history: list[dict[str, str]] | None = None,
    images: list[str] | None = None,
) -> dict[str, Any]:
    s = get_settings()
    headers = {
        "Authorization": f"Bearer {s.openrouter_api_key}",
        "Content-Type": "application/json",
        "HTTP-Referer": s.openrouter_http_referer or "https://vektral.local",
        "X-Title": s.openrouter_x_title or "Vektral",
    }
    messages: list[dict[str, Any]] = [{"role": "system", "content": system}]
    for item in history or []:
        role = str(item.get("role") or "user")
        if role not in ("user", "assistant"):
            role = "user"
        content = str(item.get("content") or "")
        if content:
            messages.append({"role": role, "content": content})
    messages.append({"role": "user", "content": _user_content(user, images)})
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
    "You are Vektral's coding agent for an existing live website. "
    "Edit the existing site in place. Do not scaffold a new app unless asked.\n"
    "Respond ONLY with a single JSON object (no markdown commentary) shaped as:\n"
    "{\n"
    '  "summary": "short description of changes",\n'
    '  "files": [{"path": "relative/path.tsx", "content": "full file contents", "pane_ids": ["optional hint"]}],\n'
    '  "commit_message": "concise commit message",\n'
    '  "done": true\n'
    "}\n"
    "Rules:\n"
    "- Return FULL file contents for each edited file.\n"
    "- Prefer minimal edits that satisfy the user request.\n"
    "- pane_ids on files are optional hints; the server decides which panes reload.\n"
    "- Never emit world coordinates or spatial fields.\n"
)


async def _mistral_chat(
    *,
    model: str,
    system: str,
    user: str,
    history: list[dict[str, str]] | None = None,
    images: list[str] | None = None,
) -> dict[str, Any]:
    s = get_settings()
    headers = {
        "Authorization": f"Bearer {s.mistral_api_key}",
        "Content-Type": "application/json",
    }
    messages: list[dict[str, Any]] = [{"role": "system", "content": system}]
    for item in history or []:
        role = str(item.get("role") or "user")
        if role not in ("user", "assistant"):
            role = "user"
        content = str(item.get("content") or "")
        if content:
            messages.append({"role": role, "content": content})
    messages.append({"role": "user", "content": _user_content(user, images)})
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
    images: list[str] | None = None,
) -> dict[str, Any]:
    """Mistral when configured; otherwise OpenRouter."""
    s = get_settings()
    if s.mistral_api_key:
        return await _mistral_chat(
            model=model, system=system, user=user, history=history, images=images
        )
    if s.openrouter_api_key:
        return await _openrouter_chat(
            model=model, system=system, user=user, history=history, images=images
        )
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
    "package.json",
    "index.html",
    "src/App.tsx",
    "src/App.jsx",
    "src/main.tsx",
    "src/main.jsx",
    "src/pages/index.tsx",
    "App.tsx",
)
_MAX_FILES = 6
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


async def gather_repo_context_for_targets(
    workspace_id: str,
    targets: list[dict[str, Any]],
    utterance: str = "",
    kind: str = "vite-spa",
    workdir: str = ".",
    create_paths: list[str] | None = None,
) -> tuple[str, list[dict[str, Any]]]:
    from app.services.source_locators import candidates_for, validate_create_paths

    per_target = max(2, _MAX_FILES // max(1, len(targets)))
    wanted: list[tuple[str, str, str]] = []  # path, pane_id, reason
    allowed_create: list[str] = []
    for target in targets:
        pid = str(target.get("pane_id") or "")
        route = str(target.get("route") or "/")
        for path in candidates_for(kind, route, workdir, utterance)[: per_target + 4]:
            wanted.append((path, pid, "locator"))
        if create_paths:
            allowed_create.extend(
                validate_create_paths(kind, route, list(create_paths), workdir)
            )
    primary_pid = str((targets[0] or {}).get("pane_id") or "") if targets else ""
    for path in dict.fromkeys(allowed_create):
        wanted.append((path, primary_pid, "create"))

    picked: dict[str, dict[str, Any]] = {}
    for path, pane_id, reason in wanted:
        rec = picked.setdefault(
            path,
            {"path": path, "candidate_pane_ids": [], "source_reason": reason},
        )
        if pane_id and pane_id not in rec["candidate_pane_ids"]:
            rec["candidate_pane_ids"].append(pane_id)
        if len(picked) >= _MAX_FILES * 2:
            break

    chunks: list[str] = []
    used: list[dict[str, Any]] = []
    total = 0
    create_set = set(allowed_create)
    for rec in picked.values():
        if len(used) >= _MAX_FILES:
            break
        resp = await git_file(workspace_id, rec["path"])
        exists = bool(resp.get("ok"))
        content = str(resp.get("content") or "") if exists else ""
        if not content and rec["path"] not in create_set:
            continue
        if not content:
            content = "(new file — create this path only if it is in the server allowlist)"
        if len(content) > _MAX_FILE_CHARS:
            content = content[:_MAX_FILE_CHARS] + "\n/* …truncated */\n"
        attribution = ",".join(rec["candidate_pane_ids"]) or "shared"
        piece = f"--- {rec['path']} (panes: {attribution}) ---\n{content}"
        if total + len(piece) > _MAX_TOTAL_CHARS:
            remain = _MAX_TOTAL_CHARS - total
            if remain < 200:
                break
            piece = piece[:remain] + "\n/* …truncated */\n"
        chunks.append(piece)
        used.append(rec)
        total += len(piece)
    if not chunks:
        return "", []
    return "Repo files:\n" + "\n\n".join(chunks), used


def _evidence_blocks(
    targets: list[dict[str, Any]],
    captures: list[dict[str, Any]],
    repo_meta: list[dict[str, Any]],
    kind: str,
    workdir: str,
) -> str:
    by_pane = {
        str(c.get("pane_id") or ""): c
        for c in captures
        if isinstance(c, dict) and c.get("pane_id")
    }
    files_by_pane: dict[str, list[str]] = {}
    for rec in repo_meta:
        path = str(rec.get("path") or "")
        for pid in rec.get("candidate_pane_ids") or []:
            files_by_pane.setdefault(str(pid), []).append(path)
    blocks: list[str] = []
    for target in targets:
        pid = str(target.get("pane_id") or "")
        cap = by_pane.get(pid) or {}
        cap_status = "available" if cap.get("available") or cap.get("image_ref") else "missing"
        vt = str(cap.get("visible_text") or "")[:500]
        files = files_by_pane.get(pid) or []
        blocks.append(
            f"Target pane_id={pid} title={target.get('title') or pid} "
            f"route={target.get('route') or '/'} runtime={kind} workdir={workdir} "
            f"capture={cap_status}\nvisible_text: {vt or '(none)'}\n"
            f"candidate_files: {', '.join(files) or '(none)'}"
        )
    return "\n\n".join(blocks)


async def run_coding_job(job: dict[str, Any], uid: str) -> dict[str, Any]:
    """Run LLM → Collab atomic mutation → selective remount/restart → ready."""
    from app.agent_kv import get_doc
    from app.services import preview as preview_svc
    from app.services.captures import model_image_payloads
    from app.services.preview_client import apply_workspace_mutation, git_head
    from app.services.source_locators import invalidate_panes, needs_restart

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
    if not is_site_ready(ws):
        err = str(ws.get("site_error") or "").strip()
        return _fail(
            job,
            err
            or "Website starter is not ready yet. Wait until the seed commit is merged.",
        )

    token = github_token_for_user(uid) or s.github_token_fallback
    if not token:
        return _fail(job, "connect GitHub with repo scope first (or set GITHUB_TOKEN)")

    branch = (
        str(job.get("branch") or "")
        or str(ws.get("vektral_branch") or "")
        or branch_name(workspace_id)
    )
    job["branch"] = branch

    snap = job.get("coding_targets") if isinstance(job.get("coding_targets"), dict) else {}
    targets = [t for t in (snap.get("targets") or []) if isinstance(t, dict)]
    if not targets:
        return _fail(job, "NO_CODING_TARGETS", extra_log="coding_targets missing on job")
    job["mutation_error_code"] = ""
    primary = str(snap.get("primary_target_pane_id") or targets[0].get("pane_id") or "")
    route = str(targets[0].get("route") or job.get("route") or "/")
    job["route"] = route

    sess = get_doc("preview_sessions", workspace_id) or {}
    runtime = sess.get("runtime") if isinstance(sess.get("runtime"), dict) else {}
    kind = str(runtime.get("kind") or "vite-spa")
    workdir = str(runtime.get("workdir") or ".")
    expected = str(snap.get("base_commit_sha") or job.get("base_commit_sha") or "")
    if not expected:
        head = await git_head(workspace_id)
        expected = str(head.get("commit_sha") or "")
        snap["base_commit_sha"] = expected
        job["coding_targets"] = snap
        job["base_commit_sha"] = expected

    provider = _llm_provider_label()
    _save(job, "planning")
    _append_log(job, f"Calling {provider} for edits ({len(targets)} target pane(s))…")
    put_doc("jobs", job["id"], job)

    create_paths = [
        str(p)
        for p in (snap.get("create_paths") or job.get("create_paths") or [])
        if str(p)
    ]
    repo_blob, repo_meta = await gather_repo_context_for_targets(
        workspace_id,
        targets,
        str(job.get("command_text") or ""),
        kind,
        workdir,
        create_paths=create_paths,
    )
    allow_paths = {str(m.get("path") or "") for m in repo_meta if m.get("path")}
    if create_paths:
        from app.services.source_locators import validate_create_paths

        route_for_create = str(targets[0].get("route") or job.get("route") or "/")
        allow_paths.update(
            validate_create_paths(kind, route_for_create, create_paths, workdir)
        )
    if repo_meta:
        _append_log(job, "Repo context: " + ", ".join(str(m["path"]) for m in repo_meta))
        job["source_files"] = repo_meta
        put_doc("jobs", job["id"], job)

    history = panes_svc.history_for_llm(
        str(job.get("chat_pane_id") or ""),
        exclude_text=str(job.get("command_text") or ""),
    )
    utterance = str(job.get("command_text") or "")
    screen_ctx = str(job.get("screen_context") or "").strip()
    captures = job.get("captures") if isinstance(job.get("captures"), list) else []
    image_urls, vision_status = model_image_payloads(captures, uid, workspace_id)
    job["vision_status"] = vision_status
    if vision_status != "ok":
        _append_log(job, "vision_unavailable — continuing with text/repository context")

    evidence = _evidence_blocks(targets, captures, repo_meta, kind, workdir)
    prefixes: list[str] = []
    if screen_ctx:
        prefixes.append(screen_ctx)
    if evidence:
        prefixes.append("Focused coding targets:\n" + evidence)
    if repo_blob:
        prefixes.append(
            f"Runtime kind={kind} workdir={workdir}. Edit these existing files.\n\n{repo_blob}"
        )
    if prefixes:
        user_prompt = "\n\n".join(prefixes) + f"\n\nUser command:\n{utterance}"
    else:
        user_prompt = utterance

    llm = await _llm_chat(
        model=str(job.get("model") or _default_llm_model()),
        system=_SYSTEM_PROMPT + f"- Primary pane: {primary} route {route}\n",
        user=user_prompt,
        history=history,
        images=image_urls,
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
    if allow_paths:
        write_list = [f for f in write_list if f["path"] in allow_paths]
        if not write_list:
            return _fail(job, "model paths were not in the gathered allowlist")

    _save(job, "editing")
    commit_msg = str(plan.get("commit_message") or "").strip() or "vektral agent edit"
    summary = str(plan.get("summary") or "").strip() or commit_msg
    put_doc("jobs", job["id"], job)

    mutate_body = {
        "expected_commit_sha": expected,
        "branch": branch,
        "message": commit_msg,
        "files": write_list,
        "github_token": token,
        "repo_full_name": ws.get("repo_full_name") or "",
        "workdir": workdir,
    }

    wait_tries = 0
    stale_retried = False
    while True:
        mut = await apply_workspace_mutation(workspace_id, mutate_body)
        code = str(mut.get("error_code") or "")
        if code == "WAITING_FOR_WORKSPACE" and wait_tries < 4:
            wait_tries += 1
            _save(job, "waiting_for_workspace")
            _append_log(job, "waiting_for_workspace — retrying mutation")
            put_doc("jobs", job["id"], job)
            await asyncio.sleep(1.0)
            continue
        if code == "STALE_CHECKOUT" and not stale_retried:
            stale_retried = True
            _metric_stale(workspace_id)
            head = await git_head(workspace_id)
            expected = str(head.get("commit_sha") or "")
            snap["base_commit_sha"] = expected
            job["coding_targets"] = snap
            job["base_commit_sha"] = expected
            mutate_body["expected_commit_sha"] = expected
            _append_log(job, "STALE_CHECKOUT — replanning from a fresh snapshot")
            repo_blob, repo_meta = await gather_repo_context_for_targets(
                workspace_id,
                targets,
                utterance,
                kind,
                workdir,
                create_paths=create_paths,
            )
            allow_paths = {str(m.get("path") or "") for m in repo_meta if m.get("path")}
            if create_paths:
                from app.services.source_locators import validate_create_paths

                allow_paths.update(
                    validate_create_paths(
                        kind,
                        str(targets[0].get("route") or job.get("route") or "/"),
                        create_paths,
                        workdir,
                    )
                )
            job["source_files"] = repo_meta
            evidence = _evidence_blocks(targets, captures, repo_meta, kind, workdir)
            prefixes = []
            if screen_ctx:
                prefixes.append(screen_ctx)
            if evidence:
                prefixes.append("Focused coding targets:\n" + evidence)
            if repo_blob:
                prefixes.append(
                    f"Runtime kind={kind} workdir={workdir}. Edit these existing files.\n\n{repo_blob}"
                )
            user_prompt = (
                "\n\n".join(prefixes) + f"\n\nUser command:\n{utterance}"
                if prefixes
                else utterance
            )
            llm = await _llm_chat(
                model=str(job.get("model") or _default_llm_model()),
                system=_SYSTEM_PROMPT + f"- Primary pane: {primary} route {route}\n",
                user=user_prompt,
                history=history,
                images=image_urls,
            )
            if not bool(llm.get("ok")):
                return _fail(job, str(llm.get("error") or f"{provider} call failed"))
            plan = extract_edit_plan(str(llm.get("content") or ""))
            if plan is None:
                return _fail(job, "model did not return valid JSON edit plan after stale replan")
            write_list = files_from_plan(plan)
            if allow_paths:
                write_list = [f for f in write_list if f["path"] in allow_paths]
            if not write_list:
                return _fail(job, "stale replan produced no allowlisted file edits")
            commit_msg = str(plan.get("commit_message") or "").strip() or commit_msg
            summary = str(plan.get("summary") or "").strip() or commit_msg
            mutate_body["files"] = write_list
            mutate_body["message"] = commit_msg
            continue
        break

    if not bool(mut.get("ok")):
        job["mutation_error_code"] = str(mut.get("error_code") or "MUTATION_FAILED")
        return _fail(
            job,
            str(mut.get("error") or mut.get("error_code") or "mutation failed"),
            extra_log=str(mut.get("logs") or "")[-2000:],
        )

    commit_sha = str(mut.get("commit_sha") or "")
    changed_paths = [str(p) for p in (mut.get("changed_paths") or [f["path"] for f in write_list])]
    job["commit_sha"] = commit_sha
    _append_log(job, f"Committed {commit_sha}: {summary}")

    restart = needs_restart(kind, changed_paths)
    runtime_ids = panes_svc.runtime_pane_ids(workspace_id, str(runtime.get("runtimeId") or job.get("runtime_id") or ""))
    affected = invalidate_panes(
        kind=kind,
        changed_paths=changed_paths,
        targets=targets,
        runtime_pane_ids=runtime_ids,
    )
    job["affected_pane_ids"] = affected

    if restart:
        _save(job, "restarting_preview")
        _append_log(job, "Restarting preview (config/lockfile change)…")
        put_doc("jobs", job["id"], job)
        prev = await preview_svc.restart_preview(workspace_id, uid)
        if str(prev.get("status") or "") == "error":
            return _fail(job, str(prev.get("error") or "preview restart failed"))

    routes: list[str] = []
    for pid in affected:
        pane = panes_svc.find_pane(pid)
        if pane:
            routes.append(str(pane.get("route") or "/"))
    ready = await preview_svc.wait_preview_routes(workspace_id, routes or ["/"])
    if not ready:
        return _fail(job, "preview runtime or routes not ready")

    versions: list[int] = []
    for pid in affected:
        bumped = panes_svc.bump_pane_reload(pid)
        if bumped:
            versions.append(int(bumped.get("reload_version") or 0))

    job["preview_url"] = ""
    job["reload_version"] = max(versions) if versions else int(job.get("reload_version") or 0)
    job["error"] = ""
    job["result_json"] = json.dumps(
        {
            "summary": summary,
            "files": [f["path"] for f in write_list],
            "written": changed_paths,
            "commit_sha": commit_sha,
            "branch": branch,
            "affected_pane_ids": affected,
            "restarted": restart,
            "commit_message": commit_msg,
        }
    )
    _append_log(job, "Job ready — affected panes should remount.")
    _save(job, "ready")
    return job


def _metric_stale(workspace_id: str) -> None:
    logger.info("preview_metric event=stale_checkout workspace_id=%s", workspace_id)
