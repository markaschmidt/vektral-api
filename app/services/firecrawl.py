"""Firecrawl HTTP client + scrape persistence."""

from __future__ import annotations

import json
import logging
import re
from typing import Any

import httpx

from app.config import get_settings
from app.schemas import scrape_item_view
from app.services.panes import preview_base_for
from app.services.workspaces import require_workspace_access
from app.agent_kv import new_id, now_iso, put_doc, query_eq

logger = logging.getLogger("vektral.firecrawl")


def _slugify(value: str, fallback: str = "page") -> str:
    norm = re.sub(r"[^A-Za-z0-9]+", "-", value or "").strip("-").lower()
    norm = re.sub(r"-{2,}", "-", norm)
    picked = norm if norm else fallback
    return picked[:60]


def _excerpt(markdown: str, limit: int = 600) -> str:
    text = re.sub(r"```[\s\S]*?```", " ", markdown or "")
    text = re.sub(r"!?\[([^\]]*)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"[#>*`_\[\]]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    tail = "…" if len(text) > limit else ""
    return text[:limit] + tail


async def _fc_post(path: str, payload: dict[str, Any]) -> dict[str, Any]:
    s = get_settings()
    if not s.firecrawl_api_key:
        raise ValueError("FIRECRAWL_API_KEY not configured")
    url = f"{s.firecrawl_base_url}{path}"
    headers = {
        "Authorization": f"Bearer {s.firecrawl_api_key}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    async with httpx.AsyncClient(timeout=120.0) as client:
        resp = await client.post(url, json=payload, headers=headers)
        raw = resp.text
        try:
            parsed = resp.json() if raw else {}
        except Exception:  # noqa: BLE001
            parsed = {}
        if resp.status_code >= 400:
            raise httpx.HTTPStatusError(
                f"Firecrawl HTTP {resp.status_code}: {raw[:400]}",
                request=resp.request,
                response=resp,
            )
        if isinstance(parsed, dict):
            return parsed
        return {"data": parsed}


def _workspace_scrapes(workspace_id: str) -> list[dict[str, Any]]:
    docs = query_eq("scrapes", "workspace_id", workspace_id)
    docs.sort(key=lambda d: d.get("created_at") or "")
    return docs


def _store_scrape(
    workspace_id: str, url: str, title: str, markdown: str, meta: str
) -> dict[str, Any] | None:
    if not workspace_id:
        return None
    slug = _slugify(title if title else url)
    existing: dict[str, Any] | None = None
    for s in _workspace_scrapes(workspace_id):
        if s.get("url") == url:
            existing = s
            break
    if existing is None:
        taken = {s.get("slug") for s in _workspace_scrapes(workspace_id)}
        unique = slug
        n = 2
        while unique in taken:
            unique = f"{slug}-{n}"
            n += 1
        sid = new_id("scrape_")
        existing = {
            "id": sid,
            "workspace_id": workspace_id,
            "url": url,
            "slug": unique,
            "created_at": now_iso(),
        }
    existing["title"] = title if title else url
    existing["markdown"] = markdown
    existing["excerpt"] = _excerpt(markdown)
    existing["metadata_json"] = meta
    existing["updated_at"] = now_iso()
    put_doc("scrapes", existing["id"], existing)
    return existing


async def scrape_and_store(
    url: str,
    formats: list[str] | None = None,
    only_main_content: bool = True,
    workspace_id: str = "",
) -> dict[str, Any]:
    if not url:
        return {"ok": False, "error": "url is required", "url": url}
    fmts = formats if formats else ["markdown", "html"]
    try:
        parsed = await _fc_post(
            "/v1/scrape",
            {"url": url, "formats": fmts, "onlyMainContent": only_main_content},
        )
        data = parsed.get("data", parsed)
        if not isinstance(data, dict):
            data = {}
        md = str(data.get("markdown", "") or "")
        html = str(data.get("html", "") or "")
        meta_obj = data.get("metadata", {})
        title = ""
        meta_json = ""
        if isinstance(meta_obj, dict):
            title = str(meta_obj.get("title", "") or "")
            meta_json = json.dumps(meta_obj)
        saved = _store_scrape(workspace_id, url, title, md, meta_json)
        return {
            "ok": True,
            "error": "",
            "url": url,
            "markdown": md,
            "html": html,
            "title": title,
            "metadata_json": meta_json,
            "slug": (saved or {}).get("slug", ""),
            "scrape_id": (saved or {}).get("id", ""),
            "job_id": "",
        }
    except ValueError as exc:
        return {"ok": False, "error": str(exc), "url": url}
    except httpx.HTTPStatusError as exc:
        return {"ok": False, "error": str(exc), "url": url}
    except Exception as exc:  # noqa: BLE001
        logger.exception("firecrawl scrape failed")
        return {"ok": False, "error": str(exc), "url": url}


async def firecrawl_scrape(
    url: str,
    formats: list[str] | None = None,
    only_main_content: bool = True,
    workspace_id: str = "",
    owner_uid: str = "",
) -> dict[str, Any]:
    result = await scrape_and_store(
        url=url,
        formats=formats,
        only_main_content=only_main_content,
        workspace_id=workspace_id,
    )
    if not result.get("ok"):
        return result
    # Record a completed job for audit trail
    from app.services.jobs import create_job_record

    job = create_job_record(
        owner_uid=owner_uid,
        status="ready",
        command_type="firecrawl_scrape",
        command_text=url,
        workspace_id=workspace_id,
        result_json=json.dumps({"url": url, "title": result.get("title", "")}),
    )
    result["job_id"] = job["id"]
    return result


def list_scrapes(workspace_id: str, uid: str) -> dict[str, Any]:
    require_workspace_access(workspace_id, uid)
    base = preview_base_for(workspace_id)
    items = [scrape_item_view(s, base) for s in _workspace_scrapes(workspace_id)]
    return {
        "ok": True,
        "error": "",
        "workspace_id": workspace_id,
        "index_route": "/research/index.html",
        "index_url": f"{base.rstrip('/')}/research/index.html" if base else "",
        "items": items,
    }


def scrape_payload(workspace_id: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for s in _workspace_scrapes(workspace_id):
        out.append(
            {
                "slug": s.get("slug"),
                "title": s.get("title"),
                "url": s.get("url"),
                "markdown": s.get("markdown"),
                "excerpt": s.get("excerpt"),
                "created_at": s.get("created_at"),
            }
        )
    return out


async def firecrawl_crawl_start(
    url: str, workspace_id: str = "", limit: int = 10, owner_uid: str = ""
) -> dict[str, Any]:
    if not url:
        return {"ok": False, "error": "url is required", "job_id": "", "status": ""}
    if not get_settings().firecrawl_api_key:
        return {
            "ok": False,
            "error": "FIRECRAWL_API_KEY not configured",
            "job_id": "",
            "status": "",
        }
    from app.services.jobs import create_job_record

    job = create_job_record(
        owner_uid=owner_uid,
        status="queued",
        command_type="firecrawl_crawl",
        command_text=url,
        workspace_id=workspace_id,
        payload_json=json.dumps({"limit": limit}),
    )
    return {"ok": True, "error": "", "job_id": job["id"], "status": "queued"}
