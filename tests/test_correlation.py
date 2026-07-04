"""Task 5 — correlation: dedup signals into exactly one incident per outage.

The first qualifying signal opens an incident immediately (speed is the product);
later signals sharing the correlation family (fingerprint) within the window join
it. The event_alert + issue of one run must collapse into ONE incident — multiple
briefs per outage destroy credibility (PRD Risk).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select

from culprit.config import REPO_ROOT, get_settings
from culprit.correlation import correlate_signal
from culprit.ingest.sentry import ingest_sentry
from culprit.models import Incident, Signal
from harness.runrecord import load_all_run_records

RUNS = load_all_run_records()
WINDOW = 600
_T0 = datetime(2026, 7, 4, 3, 45, tzinfo=UTC)


async def _add_signal(
    session, dedup_key, *, release, fingerprint, received_at, count=None
):
    sig = Signal(
        source="sentry",
        kind="event_alert",
        dedup_key=dedup_key,
        release=release,
        fingerprint=fingerprint,
        frames=[],
        count=count,
        received_at=received_at,
    )
    session.add(sig)
    await session.flush()
    return sig


async def _incident_count(session) -> int:
    return (await session.execute(select(func.count(Incident.id)))).scalar_one()


async def test_alarm_joins_open_sentry_incident_cross_source(db_session):
    """Cross-source dedup (plan decision 7): a CloudWatch alarm within the window
    of an open Sentry incident joins it (different fingerprints) -> 1 incident."""
    sentry = await _add_signal(
        db_session,
        "sentry-1",
        release="R",
        fingerprint="ConnectionError: boom",
        received_at=_T0,
    )
    inc1 = await correlate_signal(db_session, sentry, WINDOW)

    alarm = Signal(
        source="cloudwatch",
        kind="alarm",
        dedup_key="sns:alarm-1",
        release=None,
        fingerprint="tcf-prod-elasticache-health",  # differs from the Sentry title
        frames=[],
        received_at=_T0 + timedelta(seconds=30),
    )
    db_session.add(alarm)
    await db_session.flush()
    inc2 = await correlate_signal(db_session, alarm, WINDOW)

    assert inc1.id == inc2.id  # the alarm joined, did not open a second incident
    assert await _incident_count(db_session) == 1


async def test_alarm_opens_its_own_incident_outside_the_window(db_session):
    sentry = await _add_signal(
        db_session, "sentry-2", release="R", fingerprint="Boom", received_at=_T0
    )
    await correlate_signal(db_session, sentry, WINDOW)
    alarm = Signal(
        source="cloudwatch",
        kind="alarm",
        dedup_key="sns:alarm-2",
        release=None,
        fingerprint="tcf-prod-rds-connections",
        frames=[],
        received_at=_T0 + timedelta(seconds=WINDOW + 60),  # past the window
    )
    db_session.add(alarm)
    await db_session.flush()
    await correlate_signal(db_session, alarm, WINDOW)
    assert await _incident_count(db_session) == 2


async def test_two_same_release_signals_close_in_time_one_incident(db_session):
    s1 = await _add_signal(
        db_session, "a", release="R", fingerprint="Boom", received_at=_T0
    )
    s2 = await _add_signal(
        db_session,
        "b",
        release="R",
        fingerprint="Boom",
        received_at=_T0 + timedelta(seconds=5),
    )
    i1 = await correlate_signal(db_session, s1, WINDOW)
    i2 = await correlate_signal(db_session, s2, WINDOW)
    assert i1.id == i2.id
    assert await _incident_count(db_session) == 1


async def test_two_signals_far_apart_two_incidents(db_session):
    s1 = await _add_signal(
        db_session, "a", release="R", fingerprint="Boom", received_at=_T0
    )
    s2 = await _add_signal(
        db_session,
        "b",
        release="R",
        fingerprint="Boom",
        received_at=_T0 + timedelta(minutes=20),
    )
    await correlate_signal(db_session, s1, WINDOW)
    await correlate_signal(db_session, s2, WINDOW)
    assert await _incident_count(db_session) == 2


async def test_different_fingerprints_two_incidents(db_session):
    s1 = await _add_signal(
        db_session, "a", release="R", fingerprint="Boom", received_at=_T0
    )
    s2 = await _add_signal(
        db_session,
        "b",
        release="R",
        fingerprint="Kaboom",
        received_at=_T0 + timedelta(seconds=5),
    )
    await correlate_signal(db_session, s1, WINDOW)
    await correlate_signal(db_session, s2, WINDOW)
    assert await _incident_count(db_session) == 2


async def test_issue_without_release_joins_event_alert_incident(db_session):
    """The issue payload carries no release; it still joins by fingerprint."""
    ea = await _add_signal(
        db_session, "a", release="R", fingerprint="Boom", received_at=_T0
    )
    issue = await _add_signal(
        db_session,
        "b",
        release=None,
        fingerprint="Boom",
        received_at=_T0 + timedelta(seconds=2),
    )
    i1 = await correlate_signal(db_session, ea, WINDOW)
    i2 = await correlate_signal(db_session, issue, WINDOW)
    assert i1.id == i2.id
    assert await _incident_count(db_session) == 1


async def test_severity_rises_with_count(db_session):
    s = await _add_signal(
        db_session, "a", release="R", fingerprint="Boom", received_at=_T0, count=250
    )
    incident = await correlate_signal(db_session, s, WINDOW)
    assert incident.severity == 3


async def test_duplicate_correlation_is_idempotent(db_session):
    s = await _add_signal(
        db_session, "a", release="R", fingerprint="Boom", received_at=_T0
    )
    i1 = await correlate_signal(db_session, s, WINDOW)
    i2 = await correlate_signal(db_session, s, WINDOW)  # signal already has incident_id
    assert i1.id == i2.id
    assert await _incident_count(db_session) == 1


# --- integration over real fixtures ----------------------------------------


def _ea_is_run():
    for run in RUNS:
        kinds = {
            "ea" if "event_alert" in fp else "is" if "/issue/" in fp else "?"
            for fp in run.fixture_paths
        }
        if {"ea", "is"} <= kinds:
            return run
    return None


def _raw_and_ts(rel: str) -> tuple[bytes, datetime]:
    env = json.loads((REPO_ROOT / rel).read_text())
    return env["raw_body"].encode("latin-1"), datetime.fromisoformat(env["received_at"])


async def test_real_run_event_alert_and_issue_collapse(db_session):
    run = _ea_is_run()
    assert run is not None, "expected a run with both event_alert and issue fixtures"
    incident_ids = set()
    for rel in run.fixture_paths:
        if "sentry" not in rel:
            continue
        raw, ts = _raw_and_ts(rel)
        signal = await ingest_sentry(db_session, raw, ts)
        incident = await correlate_signal(db_session, signal, WINDOW)
        incident_ids.add(incident.id)
    assert len(incident_ids) == 1
    assert await _incident_count(db_session) == 1


@pytest.mark.skipif(
    not get_settings().sentry_client_secret,
    reason="SENTRY_CLIENT_SECRET not configured",
)
async def test_route_collapses_event_alert_and_issue_end_to_end(client, db_session):
    """Through POST /ingest/sentry: one run's two webhooks -> one incident."""
    run = _ea_is_run()
    assert run is not None
    for rel in run.fixture_paths:
        if "sentry" not in rel:
            continue
        env = json.loads((REPO_ROOT / rel).read_text())
        resp = await client.post(
            "/ingest/sentry",
            content=env["raw_body"].encode("latin-1"),
            headers={
                "sentry-hook-signature": env["headers"]["sentry-hook-signature"],
                "content-type": "application/json",
            },
        )
        assert resp.status_code == 200
    assert await _incident_count(db_session) == 1
