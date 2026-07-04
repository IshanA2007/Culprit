"""Resolution — flip an incident to resolved and capture the fixing commit.

``resolve_incident`` is the single writer of resolution state (M4 plan decision
2): ``status`` -> resolved, ``resolved_at``, ``resolution_source``, and the
fixing commit captured from the deploy feed. Three triggers converge here — the
operator/eval path (``POST /incidents/{id}/resolve`` + ``culprit resolve``), the
SNS ``ALARM -> OK`` auto-detect, and the Discord ``/resolve`` interaction — so
resolution behaves identically however it was signalled.

The fixing commit (decision 3) is the most recent deploy whose ``run_started_at``
is AFTER the incident opened: the green deploy that shipped the fix/rollback once
the outage began. When no post-open deploy exists (an infra fault fixed by
restarting Redis / scaling the task — no code shipped), ``fixing_sha`` stays NULL
and the postmortem states "resolved via infrastructure remediation". That is the
fix-side parallel to culprit abstention: not every resolution has a code fix.

Idempotent: resolving an already-resolved incident is a no-op — it never
re-captures the fixing commit nor re-stamps the source (the deploy-feed-is-truth
and one-outcome-per-outage stances).
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from culprit.models import Deploy, Incident


async def _capture_fix_commit(session: AsyncSession, incident: Incident) -> str | None:
    """The head SHA of the most recent deploy shipped after the incident opened.

    Returns ``None`` when the incident never opened at a known time or no deploy
    followed it — an honest "no code fix" (infra remediation), never a guess.
    """
    if incident.opened_at is None:
        return None
    deploy = (
        await session.execute(
            select(Deploy)
            .where(Deploy.run_started_at > incident.opened_at)
            .order_by(Deploy.run_started_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    return deploy.head_sha if deploy is not None else None


async def resolve_incident(
    session: AsyncSession,
    incident: Incident,
    *,
    source: str,
    resolved_at: datetime | None = None,
) -> Incident:
    """Mark ``incident`` resolved and capture its fixing commit. Idempotent."""
    if incident.status == "resolved":
        return incident

    incident.status = "resolved"
    incident.resolved_at = resolved_at or datetime.now(UTC)
    incident.resolution_source = source
    incident.fixing_sha = await _capture_fix_commit(session, incident)
    await session.commit()
    return incident
