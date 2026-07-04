"""Parse SNS CloudWatch-alarm notifications into ``Signal`` rows + the handshake.

A CloudWatch alarm delivered over SNS maps onto the existing ``signals`` schema
with **no change** (plan decision 1): ``source="cloudwatch"``, ``kind="alarm"``,
``dedup_key="sns:<MessageId>"``, ``fingerprint=<AlarmName>``, ``frames=[]``,
``release=NULL``. Idempotent on ``dedup_key`` (duplicate SNS deliveries never
double-insert).

``SubscriptionConfirmation`` is handled by GETting the (allowlisted)
``SubscribeURL`` — recorded as a ``jobs`` row for audit, never a Signal.
Signature verification happens at the endpoint; by here the bytes are trusted.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime

import httpx
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from culprit.models import Job, Signal
from culprit.sns_verify import is_allowed_sns_url


@dataclass
class ParsedSnsSignal:
    source: str
    kind: str
    dedup_key: str
    fingerprint: str | None
    raw: dict = field(default_factory=dict)


def parse_sns_notification(message: dict) -> ParsedSnsSignal | None:
    """Map an SNS Notification (CloudWatch alarm) onto the Signal contract."""
    try:
        alarm = json.loads(message["Message"])
    except (KeyError, ValueError, TypeError):
        return None
    return ParsedSnsSignal(
        source="cloudwatch",
        kind="alarm",
        dedup_key=f"sns:{message.get('MessageId')}",
        fingerprint=alarm.get("AlarmName"),
        raw={"sns": message, "alarm": alarm},
    )


async def ingest_sns_notification(
    session: AsyncSession, message: dict, received_at: datetime
) -> Signal | None:
    """Idempotently persist one SNS alarm notification as a Signal."""
    parsed = parse_sns_notification(message)
    if parsed is None:
        return None
    stmt = (
        pg_insert(Signal)
        .values(
            source=parsed.source,
            kind=parsed.kind,
            dedup_key=parsed.dedup_key,
            release=None,
            fingerprint=parsed.fingerprint,
            frames=[],
            count=None,
            users=None,
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


async def confirm_subscription(
    session: AsyncSession,
    message: dict,
    *,
    client: httpx.AsyncClient | None = None,
) -> Job:
    """Complete the SubscriptionConfirmation handshake (SSRF-guarded GET).

    Validates the ``SubscribeURL`` host allowlist, GETs it (which confirms the
    subscription with SNS), and records a ``jobs`` row for audit. Not a Signal.
    """
    url = message.get("SubscribeURL")
    if not is_allowed_sns_url(url):
        raise ValueError(f"disallowed SubscribeURL host: {url!r}")

    owns = client is None
    client = client or httpx.AsyncClient(timeout=10.0)
    try:
        resp = await client.get(url)
        resp.raise_for_status()
    finally:
        if owns:
            await client.aclose()

    job = Job(
        type="sns_subscription_confirmation",
        status="done",
        payload={
            "topic_arn": message.get("TopicArn"),
            "message_id": message.get("MessageId"),
            "subscribe_url_host": url.split("/")[2] if url else None,
        },
        created_at=datetime.now(UTC),
        finished_at=datetime.now(UTC),
    )
    session.add(job)
    await session.commit()
    return job
