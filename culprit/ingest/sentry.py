"""Parse Sentry ``event_alert`` / ``issue`` webhooks into ``Signal`` rows.

``event_alert`` carries the stack frames + release (what powers culprit
matching); ``issue`` carries the count/userCount (what seeds impact). Both share
an identical ``title`` — the correlation "fingerprint family" (plan decision 8),
since the ``issue`` payload carries no ``release``.

Idempotent on ``dedup_key`` (``<kind>:<sentry id>``): replays and duplicate
deliveries never double-insert (plan decision 3).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from culprit.models import Signal


@dataclass
class ParsedSignal:
    source: str
    kind: str
    dedup_key: str
    release: str | None
    fingerprint: str | None
    frames: list[dict] = field(default_factory=list)
    count: int | None = None
    users: int | None = None
    raw: dict = field(default_factory=dict)


def _as_int(value) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _extract_frames(event: dict) -> list[dict]:
    """In-app stack frames as {file, lineno, function} (fork-relative paths).

    Only in-app frames map to fork commits (the tcf_website/* source), so those
    are what blame-matching scores against (Sentry's suspect-commits mechanism).
    """
    frames: list[dict] = []
    exception = event.get("exception") or {}
    for value in exception.get("values", []):
        stacktrace = value.get("stacktrace") or {}
        for fr in stacktrace.get("frames", []):
            if not fr.get("in_app"):
                continue
            file = fr.get("filename") or fr.get("abs_path")
            if not file:
                continue
            frames.append(
                {
                    "file": file,
                    "lineno": fr.get("lineno"),
                    "function": fr.get("function"),
                }
            )
    return frames


def parse_sentry(body: dict) -> ParsedSignal | None:
    """Parse a decoded Sentry webhook body into a ParsedSignal (or None)."""
    data = body.get("data") or {}

    if "event" in data:
        event = data["event"]
        return ParsedSignal(
            source="sentry",
            kind="event_alert",
            dedup_key=f"event_alert:{event.get('event_id')}",
            release=event.get("release"),
            fingerprint=event.get("title"),
            frames=_extract_frames(event),
            count=None,
            users=None,
            raw=body,
        )

    if "issue" in data:
        issue = data["issue"]
        return ParsedSignal(
            source="sentry",
            kind="issue",
            dedup_key=f"issue:{issue.get('id')}",
            release=None,
            fingerprint=issue.get("title"),
            frames=[],
            count=_as_int(issue.get("count")),
            users=_as_int(issue.get("userCount")),
            raw=body,
        )

    return None


async def ingest_sentry(
    session: AsyncSession, raw_body: bytes, received_at: datetime
) -> Signal | None:
    """Parse + idempotently persist one Sentry webhook. Returns the Signal row.

    Signature verification happens at the endpoint; by here the bytes are trusted.
    """
    parsed = parse_sentry(json.loads(raw_body))
    if parsed is None:
        return None

    stmt = (
        pg_insert(Signal)
        .values(
            source=parsed.source,
            kind=parsed.kind,
            dedup_key=parsed.dedup_key,
            release=parsed.release,
            fingerprint=parsed.fingerprint,
            frames=parsed.frames,
            count=parsed.count,
            users=parsed.users,
            received_at=received_at,
            raw=parsed.raw,
        )
        .on_conflict_do_nothing(index_elements=["dedup_key"])
    )
    await session.execute(stmt)
    await session.commit()

    return (
        await session.execute(
            select(Signal).where(Signal.dedup_key == parsed.dedup_key)
        )
    ).scalar_one()
