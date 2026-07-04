"""FastAPI application — the ingest surface.

Endpoints receive raw webhook bytes exactly as the M1 recorder captured them
(``harness/recorder/app.py``): verify the HMAC over the raw body, then parse.
Tasks 3–4 add ``POST /ingest/sentry`` and ``POST /ingest/github``; correlation
(Task 5) and the analysis loop hang off the persisted rows.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from culprit.config import get_settings
from culprit.correlation import correlate_signal
from culprit.db import get_session
from culprit.ingest.github import ingest_github
from culprit.ingest.sentry import ingest_sentry
from culprit.signatures import verify_github, verify_sentry

app = FastAPI(title="Culprit")

# FastAPI dependency alias — Annotated form keeps Depends() out of arg defaults.
SessionDep = Annotated[AsyncSession, Depends(get_session)]


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/ingest/sentry")
async def ingest_sentry_route(request: Request, session: SessionDep) -> dict:
    raw = await request.body()
    signature = request.headers.get("sentry-hook-signature")
    secret = get_settings().sentry_client_secret
    if not verify_sentry(secret, raw, signature):
        raise HTTPException(status_code=401, detail="invalid Sentry signature")

    signal = await ingest_sentry(session, raw, datetime.now(UTC))
    incident = await correlate_signal(
        session, signal, get_settings().correlation_window_seconds
    )
    return {
        "signal_id": signal.id if signal else None,
        "incident_id": incident.id if incident else None,
    }


@app.post("/ingest/github")
async def ingest_github_route(request: Request, session: SessionDep) -> dict:
    raw = await request.body()
    signature = request.headers.get("x-hub-signature-256")
    secret = get_settings().culprit_gh_webhook_secret
    if not verify_github(secret, raw, signature):
        raise HTTPException(status_code=401, detail="invalid GitHub signature")

    deploy = await ingest_github(session, raw)
    return {
        "deploy_id": deploy.id if deploy else None,
        "head_sha": deploy.head_sha if deploy else None,
    }
