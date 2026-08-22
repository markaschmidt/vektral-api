"""Firecrawl scrape/crawl + research publish + scrapes list."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends

from app.deps import current_user
from app.firebase_auth import VerifiedUser
from app.schemas import FirecrawlCrawlBody, FirecrawlScrapeBody
from app.services import firecrawl as fc_svc
from app.services import research as research_svc

router = APIRouter(tags=["firecrawl"])


@router.post("/api/firecrawl/scrape")
async def api_firecrawl_scrape(
    body: FirecrawlScrapeBody,
    user: Annotated[VerifiedUser, Depends(current_user)],
) -> dict[str, Any]:
    return await fc_svc.firecrawl_scrape(
        url=body.url,
        formats=body.formats,
        only_main_content=body.only_main_content,
        workspace_id=body.workspace_id,
        owner_uid=user.uid,
    )


@router.post("/api/firecrawl/crawl")
async def api_firecrawl_crawl(
    body: FirecrawlCrawlBody,
    user: Annotated[VerifiedUser, Depends(current_user)],
) -> dict[str, Any]:
    return await fc_svc.firecrawl_crawl_start(
        url=body.url,
        workspace_id=body.workspace_id,
        limit=body.limit,
        owner_uid=user.uid,
    )


@router.get("/api/workspaces/{workspace_id}/scrapes")
async def api_list_scrapes(
    workspace_id: str,
    user: Annotated[VerifiedUser, Depends(current_user)],
) -> dict[str, Any]:
    return fc_svc.list_scrapes(workspace_id, user.uid)


@router.post("/api/workspaces/{workspace_id}/research/publish")
async def api_publish_research(
    workspace_id: str,
    user: Annotated[VerifiedUser, Depends(current_user)],
) -> dict[str, Any]:
    published = await research_svc.publish_research(workspace_id, user.uid)
    result = fc_svc.list_scrapes(workspace_id, user.uid)
    if not bool(published.get("ok", False)):
        result["ok"] = False
        result["error"] = str(published.get("error", "publish failed"))
    return result
