"""Task 2 — every model round-trips through Postgres, jsonb columns included.

Mirrors harness/runrecord.py's self-describing style: flat records that read back
exactly as written, so downstream code (correlation, ranking, eval) can trust the
persisted shape.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select

from culprit.models import Deploy, Evidence, Incident, Job, Signal

_NOW = datetime(2026, 7, 4, 3, 45, tzinfo=UTC)


async def test_deploy_roundtrip(db_session):
    db_session.add(
        Deploy(
            head_sha="a" * 40,
            previous_head_sha="b" * 40,
            branch="master",
            conclusion="success",
            run_started_at=_NOW,
            updated_at=_NOW,
            raw={"workflow": "AWS Deployment"},
        )
    )
    await db_session.commit()
    got = (
        await db_session.execute(select(Deploy).where(Deploy.head_sha == "a" * 40))
    ).scalar_one()
    assert got.id is not None
    assert got.previous_head_sha == "b" * 40
    assert got.raw == {"workflow": "AWS Deployment"}


async def test_signal_roundtrip(db_session):
    db_session.add(
        Signal(
            source="sentry",
            kind="event_alert",
            dedup_key="evt-123",
            release="deadbeef",
            fingerprint="FieldError:reviews",
            frames=[{"file": "views.py", "lineno": 42, "function": "browse"}],
            count=17,
            users=3,
            received_at=_NOW,
            raw={"event": {"event_id": "evt-123"}},
        )
    )
    await db_session.commit()
    got = (
        await db_session.execute(select(Signal).where(Signal.dedup_key == "evt-123"))
    ).scalar_one()
    assert got.frames == [{"file": "views.py", "lineno": 42, "function": "browse"}]
    assert got.count == 17
    assert got.users == 3
    assert got.incident_id is None


async def test_incident_roundtrip(db_session):
    db_session.add(
        Incident(
            opened_at=_NOW,
            status="open",
            release="deadbeef",
            correlation_key="deadbeef:FieldError",
            severity=2,
            verdict="culprit",
            ranked=[{"sha": "c" * 40, "score": 3.0, "reason": "2 frames blame it"}],
            brief_message_id="msg-1",
        )
    )
    await db_session.commit()
    got = (
        await db_session.execute(
            select(Incident).where(Incident.correlation_key == "deadbeef:FieldError")
        )
    ).scalar_one()
    assert got.status == "open"
    assert got.ranked[0]["sha"] == "c" * 40
    assert got.verdict == "culprit"


async def test_evidence_roundtrip(db_session):
    inc = Incident(status="open", release="r")
    db_session.add(inc)
    await db_session.flush()
    db_session.add(
        Evidence(
            incident_id=inc.id,
            commit_sha="d" * 40,
            kind="blame",
            payload={"file": "views.py", "lineno": 42, "sha": "d" * 40},
            cited=True,
        )
    )
    await db_session.commit()
    got = (
        await db_session.execute(
            select(Evidence).where(Evidence.commit_sha == "d" * 40)
        )
    ).scalar_one()
    assert got.kind == "blame"
    assert got.cited is True
    assert got.payload["lineno"] == 42


async def test_job_roundtrip(db_session):
    db_session.add(
        Job(
            type="analyze",
            status="pending",
            attempts=0,
            payload={"incident_id": 1},
            created_at=_NOW,
        )
    )
    await db_session.commit()
    got = (
        await db_session.execute(select(Job).where(Job.type == "analyze"))
    ).scalar_one()
    assert got.status == "pending"
    assert got.attempts == 0
    assert got.payload == {"incident_id": 1}


async def test_incident_signals_relationship(db_session):
    inc = Incident(status="open", release="deadbeef")
    db_session.add(inc)
    await db_session.flush()
    db_session.add_all(
        [
            Signal(
                source="sentry", kind="event_alert", dedup_key="e1", incident_id=inc.id
            ),
            Signal(source="sentry", kind="issue", dedup_key="i1", incident_id=inc.id),
        ]
    )
    await db_session.commit()

    got = (
        await db_session.execute(select(Incident).where(Incident.id == inc.id))
    ).scalar_one()
    await db_session.refresh(got, attribute_names=["signals"])
    assert len(got.signals) == 2
    assert {s.kind for s in got.signals} == {"event_alert", "issue"}
