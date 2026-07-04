"""Parse GitHub ``workflow_run`` ("AWS Deployment") webhooks into ``Deploy`` rows.

This is the deploy timeline, NOT a trigger (HANDOFF §4): each deploy records
SHA + timestamps so the window can be reconstructed as
``compare(previous_head_sha, head_sha)``. Only the deploy workflow is recorded —
CI ``workflow_run`` events (a different ``name``) are ignored so they don't
pollute the timeline.

Idempotent on ``head_sha``: replays and duplicate deliveries upsert in place
(the deploy's mutable state stays current) rather than double-inserting
(plan decision 3).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from culprit.models import Deploy

# The deploy workflow's display name (theCourseForum2's aws.yml / the harness's
# fake-deploy.yml). Non-matching workflow_run events are not deploys.
DEPLOY_WORKFLOW_NAME = "AWS Deployment"


@dataclass
class ParsedDeploy:
    head_sha: str
    branch: str | None
    conclusion: str | None
    run_started_at: datetime | None
    updated_at: datetime | None
    raw: dict = field(default_factory=dict)


def _parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def parse_github(
    body: dict, deploy_workflow_name: str = DEPLOY_WORKFLOW_NAME
) -> ParsedDeploy | None:
    """Parse a decoded ``workflow_run`` webhook into a ParsedDeploy (or None)."""
    wr = body.get("workflow_run")
    if not wr:
        return None
    if wr.get("name") != deploy_workflow_name:
        return None
    head_sha = wr.get("head_sha")
    if not head_sha:
        return None
    return ParsedDeploy(
        head_sha=head_sha,
        branch=wr.get("head_branch"),
        conclusion=wr.get("conclusion"),
        run_started_at=_parse_ts(wr.get("run_started_at")),
        updated_at=_parse_ts(wr.get("updated_at")),
        raw=body,
    )


async def _previous_head_sha(session: AsyncSession, parsed: ParsedDeploy) -> str | None:
    """Head SHA of the prior deploy on the same branch (the window base)."""
    stmt = (
        select(Deploy.head_sha)
        .where(Deploy.branch == parsed.branch, Deploy.head_sha != parsed.head_sha)
        .order_by(Deploy.run_started_at.desc().nulls_last(), Deploy.id.desc())
    )
    if parsed.run_started_at is not None:
        stmt = stmt.where(Deploy.run_started_at < parsed.run_started_at)
    return (await session.execute(stmt.limit(1))).scalar_one_or_none()


async def ingest_github(session: AsyncSession, raw_body: bytes) -> Deploy | None:
    """Parse + idempotently upsert one deploy. Returns the Deploy row (or None)."""
    parsed = parse_github(json.loads(raw_body))
    if parsed is None:
        return None

    previous_head_sha = await _previous_head_sha(session, parsed)

    stmt = (
        pg_insert(Deploy)
        .values(
            head_sha=parsed.head_sha,
            previous_head_sha=previous_head_sha,
            branch=parsed.branch,
            conclusion=parsed.conclusion,
            run_started_at=parsed.run_started_at,
            updated_at=parsed.updated_at,
            raw=parsed.raw,
        )
        # Re-delivery keeps mutable state current but never rewrites the window base.
        .on_conflict_do_update(
            index_elements=["head_sha"],
            set_={
                "branch": parsed.branch,
                "conclusion": parsed.conclusion,
                "updated_at": parsed.updated_at,
                "raw": parsed.raw,
            },
        )
    )
    await session.execute(stmt)
    await session.commit()

    return (
        await session.execute(select(Deploy).where(Deploy.head_sha == parsed.head_sha))
    ).scalar_one()
