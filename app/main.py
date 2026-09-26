"""Vektral-API entrypoint — FastAPI only (no Jac / jac-agent)."""

from __future__ import annotations

import logging
import os

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import get_settings
from app.firebase_auth import init_firebase
from app.routers import (
    auth,
    captures,
    dialogue,
    embodiment,
    firecrawl,
    github,
    health,
    jobs,
    linear,
    orgs,
    panes,
    preview,
    session,
    sso,
    voice,
    workspaces,
)

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
logger = logging.getLogger("vektral.api")

settings = get_settings()

app = FastAPI(
    title="Vektral-API",
    version="0.3.0",
    description=(
        "Auth bridge, domain Firestore REST, Vocal Bridge token mint, and VR "
        "/api/* gateway (session, panes, preview→Collab, Firecrawl, jobs). "
        "Pure FastAPI — zero Jac dependency."
    ),
)

_origins = settings.cors_origins
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"] if _origins == ["*"] else _origins,
    allow_credentials=_origins != ["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(health.router)
app.include_router(auth.router)
app.include_router(sso.router)
app.include_router(voice.router)
app.include_router(session.router)
app.include_router(workspaces.router)
app.include_router(orgs.router)
app.include_router(github.router)
app.include_router(linear.router)
app.include_router(panes.router)
app.include_router(preview.router)
app.include_router(firecrawl.router)
app.include_router(jobs.router)
app.include_router(embodiment.router)
app.include_router(dialogue.router)
app.include_router(captures.router)


@app.on_event("startup")
def _startup() -> None:
    ok = init_firebase()
    if ok:
        logger.info("Firebase ready")
    else:
        logger.warning(
            "Running without Firebase Admin (stub mode). "
            "Set ALLOW_DEV_BEARER=true for Bearer dev:<uid> smoke tests, "
            "or mount a service account (FIREBASE.md)."
        )


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "app.main:app",
        host="0.0.0.0",
        port=settings.api_port,
        log_level=os.environ.get("LOG_LEVEL", "info").lower(),
    )
