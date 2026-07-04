"""Task 3 — Sentry ingest: verify signature, parse event_alert/issue -> Signal.

Parsing/persistence tests run with no secret. The signature-endpoint tests gate
on SENTRY_CLIENT_SECRET (present locally via .env; CI has no secret so they skip,
the M1 convention). The committed fixtures are already signed with that secret.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pytest
from sqlalchemy import func, select

from culprit.config import REPO_ROOT, get_settings
from culprit.ingest.sentry import ingest_sentry, parse_sentry
from culprit.models import Signal

EVENT_ALERT_DIR = REPO_ROOT / "fixtures" / "sentry" / "event_alert"
ISSUE_DIR = REPO_ROOT / "fixtures" / "sentry" / "issue"

SENTRY_SECRET = get_settings().sentry_client_secret
requires_secret = pytest.mark.skipif(
    not SENTRY_SECRET, reason="SENTRY_CLIENT_SECRET not configured"
)


def _fixtures(d: Path) -> list[Path]:
    return sorted(d.glob("*.json"))


def _raw(path: Path) -> bytes:
    env = json.loads(path.read_text())
    return env["raw_body"].encode("latin-1")


def _received_at(path: Path) -> datetime:
    env = json.loads(path.read_text())
    return datetime.fromisoformat(env["received_at"])


def _sig(path: Path) -> str:
    env = json.loads(path.read_text())
    return env["headers"]["sentry-hook-signature"]


# --- parse (pure) ----------------------------------------------------------


def test_parse_event_alert_extracts_release_and_in_app_frames():
    path = _fixtures(EVENT_ALERT_DIR)[0]
    body = json.loads(_raw(path))
    parsed = parse_sentry(body)
    assert parsed is not None
    assert parsed.kind == "event_alert"
    assert parsed.release and len(parsed.release) == 40
    assert parsed.frames, "expected at least one in_app frame"
    frame = parsed.frames[0]
    assert {"file", "lineno", "function"} <= set(frame)
    assert frame["file"].startswith("tcf_website/")


def test_parse_issue_extracts_counts():
    path = _fixtures(ISSUE_DIR)[0]
    body = json.loads(_raw(path))
    parsed = parse_sentry(body)
    assert parsed is not None
    assert parsed.kind == "issue"
    assert parsed.count is not None
    assert parsed.users is not None
    assert parsed.release is None  # issue payloads carry no release


# --- persist (idempotent) --------------------------------------------------


async def test_every_event_alert_ingests_one_signal_with_release_and_frames(db_session):
    fixtures = _fixtures(EVENT_ALERT_DIR)
    assert fixtures
    for path in fixtures:
        sig = await ingest_sentry(db_session, _raw(path), _received_at(path))
        assert sig is not None
        assert sig.kind == "event_alert"
        assert sig.release and len(sig.release) == 40
        assert sig.frames
    total = (await db_session.execute(select(func.count(Signal.id)))).scalar_one()
    assert total == len(fixtures)


async def test_every_issue_ingests_one_signal_with_counts(db_session):
    fixtures = _fixtures(ISSUE_DIR)
    assert fixtures
    for path in fixtures:
        sig = await ingest_sentry(db_session, _raw(path), _received_at(path))
        assert sig is not None
        assert sig.kind == "issue"
        assert sig.count is not None
    total = (await db_session.execute(select(func.count(Signal.id)))).scalar_one()
    assert total == len(fixtures)


async def test_duplicate_delivery_does_not_double_insert(db_session):
    path = _fixtures(EVENT_ALERT_DIR)[0]
    raw, ts = _raw(path), _received_at(path)
    s1 = await ingest_sentry(db_session, raw, ts)
    s2 = await ingest_sentry(db_session, raw, ts)
    assert s1.id == s2.id
    total = (
        await db_session.execute(
            select(func.count(Signal.id)).where(Signal.dedup_key == s1.dedup_key)
        )
    ).scalar_one()
    assert total == 1


# --- endpoint signature (gated) --------------------------------------------


@requires_secret
async def test_valid_signature_returns_200(client):
    path = _fixtures(EVENT_ALERT_DIR)[0]
    resp = await client.post(
        "/ingest/sentry",
        content=_raw(path),
        headers={
            "sentry-hook-signature": _sig(path),
            "sentry-hook-resource": "event_alert",
            "content-type": "application/json",
        },
    )
    assert resp.status_code == 200


@requires_secret
async def test_tampered_body_returns_401(client):
    path = _fixtures(EVENT_ALERT_DIR)[0]
    resp = await client.post(
        "/ingest/sentry",
        content=_raw(path) + b" ",  # one byte changed -> signature no longer valid
        headers={
            "sentry-hook-signature": _sig(path),
            "content-type": "application/json",
        },
    )
    assert resp.status_code == 401


@requires_secret
async def test_missing_signature_returns_401(client):
    path = _fixtures(EVENT_ALERT_DIR)[0]
    resp = await client.post(
        "/ingest/sentry",
        content=_raw(path),
        headers={"content-type": "application/json"},
    )
    assert resp.status_code == 401
