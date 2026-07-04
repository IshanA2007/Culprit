"""FastAPI application — the ingest surface.

Endpoints receive raw webhook bytes exactly as the M1 recorder captured them
(``harness/recorder/app.py``): verify the signature over the raw body, then parse.
``/ingest/sentry`` + ``/ingest/github`` (M2) and ``/ingest/sns`` (M3) persist
signals/deploys; correlation dedups them into incidents. When ``autorun_pipeline``
is set, ingest schedules the analysis loop in the background so the brief posts.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Annotated

from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from culprit.config import Settings, get_settings
from culprit.correlation import correlate_signal
from culprit.db import get_session
from culprit.ingest.github import ingest_github
from culprit.ingest.sentry import ingest_sentry
from culprit.ingest.sns import (
    alarm_state,
    confirm_subscription,
    ingest_sns_notification,
    resolve_from_alarm_ok,
)
from culprit.signatures import verify_github, verify_sentry
from culprit.sns_verify import verify_message

app = FastAPI(title="Culprit")

# FastAPI dependency alias — Annotated form keeps Depends() out of arg defaults.
SessionDep = Annotated[AsyncSession, Depends(get_session)]


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


def _schedule_pipeline(
    background_tasks: BackgroundTasks, incident_id: int | None, settings: Settings
) -> None:
    """Run the analysis loop in the background if enabled (posts the brief)."""
    if incident_id is not None and settings.autorun_pipeline:
        background_tasks.add_task(_run_pipeline_bg, incident_id)


async def _run_pipeline_bg(incident_id: int) -> None:
    """Build integrations from settings and run the pipeline for one incident."""
    from culprit.brief import DiscordClient
    from culprit.db import get_sessionmaker
    from culprit.github_api import GitHubClient
    from culprit.llm import LLM
    from culprit.models import Incident
    from culprit.pipeline import run_pipeline
    from culprit.similar import SimilarIncidentSearch, VoyageEmbedder

    settings = get_settings()
    github = GitHubClient(settings.github_token, settings.github_repo)
    llm = LLM(settings.anthropic_api_key)
    discord = DiscordClient(settings.discord_webhook_url)
    similar = SimilarIncidentSearch(VoyageEmbedder(settings.voyage_api_key))
    maker = get_sessionmaker()
    try:
        async with maker() as session:
            incident = await session.get(Incident, incident_id)
            if incident is not None:
                await run_pipeline(
                    session,
                    incident,
                    github=github,
                    llm=llm,
                    discord=discord,
                    runbook_selector=llm,
                    similar=similar,
                )
    finally:
        await github.aclose()
        await discord.aclose()


@app.post("/ingest/sentry")
async def ingest_sentry_route(
    request: Request, session: SessionDep, background_tasks: BackgroundTasks
) -> dict:
    raw = await request.body()
    signature = request.headers.get("sentry-hook-signature")
    settings = get_settings()
    if not verify_sentry(settings.sentry_client_secret, raw, signature):
        raise HTTPException(status_code=401, detail="invalid Sentry signature")

    signal = await ingest_sentry(session, raw, datetime.now(UTC))
    incident = await correlate_signal(
        session, signal, settings.correlation_window_seconds
    )
    _schedule_pipeline(background_tasks, incident.id if incident else None, settings)
    return {
        "signal_id": signal.id if signal else None,
        "incident_id": incident.id if incident else None,
    }


@app.post("/incidents/{incident_id}/resolve")
async def resolve_incident_route(
    incident_id: int, session: SessionDep, background_tasks: BackgroundTasks
) -> dict:
    """Operator-triggered resolution (a maintainer typed `resolve`).

    Flips the incident to resolved, captures the fixing commit from the deploy
    feed, and (when autorun is on) re-renders the living brief with `resolved`.
    """
    from culprit.models import Incident
    from culprit.resolution import resolve_incident

    incident = await session.get(Incident, incident_id)
    if incident is None:
        raise HTTPException(status_code=404, detail="incident not found")

    await resolve_incident(session, incident, source="manual")
    _schedule_pipeline(background_tasks, incident.id, get_settings())
    return {
        "incident_id": incident.id,
        "status": incident.status,
        "fixing_sha": incident.fixing_sha,
        "resolution_source": incident.resolution_source,
    }


@app.post("/discord/interactions")
async def discord_interactions_route(
    request: Request, session: SessionDep, background_tasks: BackgroundTasks
) -> dict:
    """Discord-native resolution: a signed ``/resolve`` slash command.

    Ed25519-verified over the raw body (401 on failure), PING answered with PONG,
    ``/resolve`` routed through the shared resolver (``source="discord"``).
    """
    from culprit.discord_verify import verify_interaction
    from culprit.ingest.discord import (
        CHANNEL_MESSAGE_WITH_SOURCE,
        PING,
        PONG,
        parse_resolve_incident_id,
    )
    from culprit.models import Incident
    from culprit.resolution import resolve_incident

    settings = get_settings()
    raw = await request.body()
    if not verify_interaction(
        settings.discord_public_key,
        request.headers.get("x-signature-timestamp"),
        raw,
        request.headers.get("x-signature-ed25519"),
    ):
        raise HTTPException(status_code=401, detail="invalid Discord signature")

    interaction = json.loads(raw)
    if interaction.get("type") == PING:
        return {"type": PONG}

    incident_id = parse_resolve_incident_id(interaction)
    if incident_id is None:
        return {
            "type": CHANNEL_MESSAGE_WITH_SOURCE,
            "data": {"content": "Unknown command."},
        }
    incident = await session.get(Incident, incident_id)
    if incident is None:
        return {
            "type": CHANNEL_MESSAGE_WITH_SOURCE,
            "data": {"content": f"Incident {incident_id} not found."},
        }
    await resolve_incident(session, incident, source="discord")
    _schedule_pipeline(background_tasks, incident.id, settings)
    fix = incident.fixing_sha[:8] if incident.fixing_sha else "none (infra remediation)"
    return {
        "type": CHANNEL_MESSAGE_WITH_SOURCE,
        "data": {
            "content": f"✅ Resolved incident {incident.id} — fixing commit: {fix}."
        },
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


def _sns_cert_pem(settings: Settings) -> bytes | None:
    """Vendored/dev cert for offline verification (unset in prod -> fetch live)."""
    if settings.sns_signing_cert_path:
        from pathlib import Path

        return Path(settings.sns_signing_cert_path).read_bytes()
    return None


@app.post("/ingest/sns")
async def ingest_sns_route(
    request: Request, session: SessionDep, background_tasks: BackgroundTasks
) -> dict:
    # SNS delivers JSON with Content-Type text/plain, so dispatch on the header,
    # never the content type (the classic gotcha).
    message_type = request.headers.get("x-amz-sns-message-type")
    if not message_type:
        raise HTTPException(status_code=400, detail="missing x-amz-sns-message-type")

    raw = await request.body()
    try:
        message = json.loads(raw)
    except ValueError as e:
        raise HTTPException(status_code=400, detail="invalid SNS body") from e

    settings = get_settings()

    allowed = settings.sns_allowed_topic_arns
    if allowed:
        permitted = {a.strip() for a in allowed.split(",") if a.strip()}
        if message.get("TopicArn") not in permitted:
            raise HTTPException(status_code=403, detail="topic not allowlisted")

    if settings.sns_signature_strict:
        ok = await verify_message(message, cert_pem=_sns_cert_pem(settings))
        if not ok:
            raise HTTPException(status_code=401, detail="invalid SNS signature")

    if message_type == "SubscriptionConfirmation":
        await confirm_subscription(session, message)
        return {"confirmed": True}
    if message_type == "UnsubscribeConfirmation":
        return {"acknowledged": True}

    # Notification. An ALARM -> OK transition auto-resolves the incident that
    # alarm opened/joined (M4 decision 2) — it never opens a new one.
    if alarm_state(message) == "OK":
        incident = await resolve_from_alarm_ok(session, message, datetime.now(UTC))
        return {"resolved_incident_id": incident.id if incident else None}

    signal = await ingest_sns_notification(session, message, datetime.now(UTC))
    incident = await correlate_signal(
        session, signal, settings.correlation_window_seconds
    )
    _schedule_pipeline(background_tasks, incident.id if incident else None, settings)
    return {
        "signal_id": signal.id if signal else None,
        "incident_id": incident.id if incident else None,
    }
