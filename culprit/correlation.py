"""Correlation window — dedup signals into exactly one incident per outage.

The first qualifying signal opens an incident immediately (plan decision 8);
later signals sharing the correlation family within ``CORRELATION_WINDOW_SECONDS``
join it and raise severity. Deterministic and testable (HANDOFF §3).

Correlation family = the signal ``fingerprint`` (the Sentry title, identical
across an outage's event_alert + issue). Release is a compatibility check — an
``issue`` (no release) joins an ``event_alert`` (release=R) because their
fingerprints match and the release is compatible (one side is null).
"""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from culprit.models import Incident, Signal


def _severity(count: int | None, users: int | None) -> int:
    """A coarse 1–3 level from the largest of count/users (deterministic)."""
    n = max(count or 0, users or 0)
    if n >= 100:
        return 3
    if n >= 10:
        return 2
    return 1


async def _find_open_incident(
    session: AsyncSession, signal: Signal, window_seconds: int
) -> Incident | None:
    if signal.received_at is None or signal.fingerprint is None:
        return None
    window_start = signal.received_at - timedelta(seconds=window_seconds)
    conds = [
        Incident.status == "open",
        Incident.correlation_key == signal.fingerprint,
        Incident.opened_at >= window_start,
    ]
    if signal.release is not None:
        conds.append(
            or_(Incident.release.is_(None), Incident.release == signal.release)
        )
    stmt = select(Incident).where(*conds).order_by(Incident.opened_at.desc()).limit(1)
    return (await session.execute(stmt)).scalars().first()


async def correlate_signal(
    session: AsyncSession, signal: Signal | None, window_seconds: int
) -> Incident | None:
    """Open or join an incident for ``signal``. Idempotent on re-correlation."""
    if signal is None:
        return None
    if signal.incident_id is not None:
        return await session.get(Incident, signal.incident_id)

    incident = await _find_open_incident(session, signal, window_seconds)
    if incident is None:
        incident = Incident(
            opened_at=signal.received_at,
            status="open",
            release=signal.release,
            correlation_key=signal.fingerprint,
            severity=_severity(signal.count, signal.users),
        )
        session.add(incident)
        await session.flush()
    else:
        if incident.release is None and signal.release:
            incident.release = signal.release
        incident.severity = max(
            incident.severity or 1, _severity(signal.count, signal.users)
        )

    signal.incident_id = incident.id
    await session.commit()
    return incident
