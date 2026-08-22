"""Research pipeline — scrape keywords → Collab publish → Research pane."""

from __future__ import annotations

import json
import re
from typing import Any

from app.services import firecrawl as fc
from app.services import panes as panes_svc
from app.services.preview_client import preview_runner_request
from app.services.workspaces import (
    branch_name,
    github_token_for_user,
    require_workspace_access,
)
from app.agent_kv import get_doc, now_iso, put_doc

RESEARCH_ROUTE = "/research/index.html"

RESEARCH_WORDS = [
    "scrape",
    "crawl",
    "research",
    "look up",
    "lookup",
    "browse",
    "read the page",
    "read this page",
    "fetch the page",
    "summarize the page",
    "pull up",
    "find information",
    "web search",
]

URL_PATTERN = re.compile(
    r"https?://[^\s<>\"'\)]+|(?<![\w@.])(?:www\.)[^\s<>\"'\)]+"
)


def extract_url(text: str) -> str:
    hit = URL_PATTERN.search(text or "")
    if hit is None:
        return ""
    found = hit.group(0).rstrip(".,;:!?)")
    if not found.startswith("http"):
        found = f"https://{found}"
    return found


def is_research_command(text: str) -> bool:
    lowered = (text or "").lower()
    if not lowered:
        return False
    has_url = bool(extract_url(lowered))
    for word in RESEARCH_WORDS:
        if word in lowered:
            return True
    if has_url:
        for verb in (
            "add ",
            "build ",
            "make ",
            "change ",
            "fix ",
            "refactor ",
            "implement ",
            "write ",
            "update ",
            "style ",
            "deploy ",
        ):
            if verb in lowered:
                return False
        return True
    return False


async def publish_research(workspace_id: str, uid: str) -> dict[str, Any]:
    ws = require_workspace_access(workspace_id, uid)
    items = fc.scrape_payload(workspace_id)
    if not items:
        return {"ok": False, "error": "nothing to publish"}
    sess = get_doc("preview_sessions", workspace_id) or {}
    token = github_token_for_user(uid)
    from app.config import get_settings

    token = token or get_settings().github_token_fallback
    resp = await preview_runner_request(
        "POST",
        "/v1/research/publish",
        {
            "workspace_id": workspace_id,
            "items": items,
            "branch": sess.get("branch") or branch_name(workspace_id),
            "github_token": token,
            "repo_full_name": ws.get("repo_full_name") or "",
            "commit": True,
        },
    )
    if not bool(resp.get("ok", False)):
        return {"ok": False, "error": str(resp.get("error", "publish failed"))}

    pane_id = ""
    for pane in panes_svc.list_panes(workspace_id, uid):
        if pane.get("route") == RESEARCH_ROUTE:
            pane_id = pane["id"]
            break
    if not pane_id:
        created = panes_svc.create_pane(
            workspace_id, uid, title="Research", route=RESEARCH_ROUTE
        )
        pane_id = created.get("id", "")
    panes_svc.bump_workspace_panes(workspace_id)
    return {
        "ok": True,
        "pane_id": pane_id,
        "route": RESEARCH_ROUTE,
        "count": int(resp.get("count", len(items)) or len(items)),
        "commit_sha": str(resp.get("commit_sha", "") or ""),
    }


async def run_research_job(job: dict[str, Any], uid: str) -> dict[str, Any]:
    url = extract_url(job.get("command_text") or "")
    if not url:
        job["status"] = "failed"
        job["error"] = "no URL found in the command"
        job["updated_at"] = now_iso()
        put_doc("jobs", job["id"], job)
        return job

    job["status"] = "running"
    job["updated_at"] = now_iso()
    put_doc("jobs", job["id"], job)

    scraped = await fc.scrape_and_store(url=url, workspace_id=job.get("workspace_id") or "")
    if not scraped.get("ok"):
        job["status"] = "failed"
        job["error"] = str(scraped.get("error", "scrape failed"))
        job["updated_at"] = now_iso()
        put_doc("jobs", job["id"], job)
        return job

    published = await publish_research(job.get("workspace_id") or "", uid)
    if not bool(published.get("ok", False)):
        job["status"] = "failed"
        job["error"] = str(published.get("error", "publish failed"))
        job["result_json"] = json.dumps(
            {
                "url": url,
                "title": scraped.get("title"),
                "slug": scraped.get("slug"),
            }
        )
        job["updated_at"] = now_iso()
        put_doc("jobs", job["id"], job)
        return job

    sess = get_doc("preview_sessions", job.get("workspace_id") or "") or {}
    base = sess.get("preview_base_url") or ""
    job["status"] = "ready"
    job["error"] = ""
    job["pane_id"] = str(published.get("pane_id") or job.get("pane_id") or "")
    job["route"] = RESEARCH_ROUTE
    job["commit_sha"] = str(published.get("commit_sha") or "")
    job["preview_url"] = f"{base.rstrip('/')}{RESEARCH_ROUTE}" if base else ""
    job["reload_version"] = int(job.get("reload_version") or 0) + 1
    job["result_json"] = json.dumps(
        {
            "url": url,
            "title": scraped.get("title"),
            "slug": scraped.get("slug"),
            "scrape_id": scraped.get("scrape_id"),
            "route": RESEARCH_ROUTE,
            "item_route": f"/research/{scraped.get('slug')}.html",
            "count": published.get("count", 0),
        }
    )
    job["updated_at"] = now_iso()
    put_doc("jobs", job["id"], job)
    return job
