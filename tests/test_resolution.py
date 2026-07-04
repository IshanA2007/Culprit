"""Task 1 — resolution: flip an incident to resolved, capture the fixing commit.

``resolve_incident`` is the single writer of resolution state (status,
resolved_at, resolution_source, fixing_sha). The fixing commit is the most recent
deploy that shipped AFTER the incident opened — or ``None`` (an infra remediation,
the fix-side parallel to culprit abstention). Idempotent: resolving an already
resolved incident never re-captures or re-stamps.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from culprit.models import Deploy, Incident
from culprit.resolution import resolve_incident

_T0 = datetime(2026, 7, 4, 3, 45, tzinfo=UTC)


async def _incident(session, *, opened_at=_T0, release=None):
    inc = Incident(
        opened_at=opened_at,
        status="open",
        release=release,
        correlation_key="Boom",
        severity=1,
    )
    session.add(inc)
    await session.flush()
    return inc


async def _deploy(session, sha, started_at, *, previous=None):
    d = Deploy(
        head_sha=sha,
        previous_head_sha=previous,
        branch="master",
        run_started_at=started_at,
    )
    session.add(d)
    await session.flush()
    return d


async def test_resolve_flips_status_and_stamps_source(db_session):
    inc = await _incident(db_session)
    await resolve_incident(db_session, inc, source="manual")
    assert inc.status == "resolved"
    assert inc.resolved_at is not None
    assert inc.resolution_source == "manual"


async def test_captures_fixing_commit_from_post_open_deploy(db_session):
    inc = await _incident(db_session)
    # a deploy that shipped AFTER the incident opened = the fix ship
    await _deploy(db_session, "f" * 40, _T0 + timedelta(minutes=10))
    await resolve_incident(db_session, inc, source="manual")
    assert inc.fixing_sha == "f" * 40


async def test_no_post_open_deploy_means_no_fixing_commit(db_session):
    inc = await _incident(db_session)
    # only a deploy from BEFORE the incident opened (the fault ship, not a fix)
    await _deploy(db_session, "a" * 40, _T0 - timedelta(minutes=5))
    await resolve_incident(db_session, inc, source="sns_ok")
    assert inc.fixing_sha is None  # infra remediation — no code fix


async def test_picks_most_recent_post_open_deploy(db_session):
    inc = await _incident(db_session)
    await _deploy(db_session, "b" * 40, _T0 + timedelta(minutes=5))
    await _deploy(db_session, "c" * 40, _T0 + timedelta(minutes=15))
    await resolve_incident(db_session, inc, source="manual")
    assert inc.fixing_sha == "c" * 40


async def test_double_resolve_is_idempotent(db_session):
    inc = await _incident(db_session)
    await _deploy(db_session, "f" * 40, _T0 + timedelta(minutes=10))
    await resolve_incident(db_session, inc, source="manual")
    first_resolved_at = inc.resolved_at
    # a later deploy arrives; a second resolve must NOT re-capture or re-stamp
    await _deploy(db_session, "g" * 40, _T0 + timedelta(minutes=30))
    await resolve_incident(db_session, inc, source="sns_ok")
    assert inc.resolved_at == first_resolved_at
    assert inc.fixing_sha == "f" * 40
    assert inc.resolution_source == "manual"


# --- the operator route (POST /incidents/{id}/resolve) ----------------------


async def test_resolve_route_flips_status(client, db_session):
    inc = await _incident(db_session)
    await db_session.commit()  # the route reads it in its own transaction
    resp = await client.post(f"/incidents/{inc.id}/resolve")
    assert resp.status_code == 200
    assert resp.json()["status"] == "resolved"
    await db_session.refresh(inc)
    assert inc.status == "resolved"
    assert inc.resolution_source == "manual"


async def test_resolve_route_404_for_unknown_incident(client):
    resp = await client.post("/incidents/999999/resolve")
    assert resp.status_code == 404
