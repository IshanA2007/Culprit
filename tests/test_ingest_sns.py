"""Task 7 — POST /ingest/sns: handshake, verify-then-parse, idempotent.

Direct tests drive parse/persist/handshake against db_session; route tests drive
the endpoint (dispatching on x-amz-sns-message-type, 401 on a bad signature). The
committed fixtures are signed by the vendored keypair, so the route verifies
against the vendored cert (CULPRIT sets SNS_SIGNING_CERT_PATH for the smoke-check;
here a fixture sets it and clears the settings cache).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import func, select

from culprit.config import REPO_ROOT, get_settings
from culprit.correlation import correlate_signal
from culprit.ingest.sentry import ingest_sentry
from culprit.ingest.sns import (
    alarm_state,
    confirm_subscription,
    ingest_sns_notification,
    parse_sns_notification,
    resolve_from_alarm_ok,
)
from culprit.models import Incident, Signal
from harness.runrecord import load_all_run_records

SNS_DIR = REPO_ROOT / "fixtures" / "sns"
CERT_PATH = REPO_ROOT / "harness" / "snsfeed_inputs" / "sns_signing_cert.pem"
_NOW = datetime(2026, 7, 4, 4, 0, tzinfo=UTC)


def _alarm_notification(alarm_name: str, state: str, message_id: str) -> dict:
    """A minimal SNS Notification carrying a CloudWatch alarm state-change."""
    return {
        "Type": "Notification",
        "MessageId": message_id,
        "Message": json.dumps({"AlarmName": alarm_name, "NewStateValue": state}),
    }


def _a_fixture_notification() -> dict:
    env = json.loads(sorted(SNS_DIR.glob("*.json"))[0].read_text())
    return json.loads(env["raw_body"].encode("latin-1"))


# --- direct parse / persist --------------------------------------------------


def test_parse_maps_alarm_to_a_cloudwatch_signal():
    parsed = parse_sns_notification(_a_fixture_notification())
    assert parsed.source == "cloudwatch"
    assert parsed.kind == "alarm"
    assert parsed.dedup_key.startswith("sns:")
    assert parsed.fingerprint.startswith("tcf-prod-")  # the AlarmName


async def test_ingest_creates_a_signal_with_no_schema_change(db_session):
    notif = _a_fixture_notification()
    signal = await ingest_sns_notification(db_session, notif, _NOW)
    assert signal.source == "cloudwatch"
    assert signal.kind == "alarm"
    assert signal.frames == []  # frameless (plan decision 1)
    assert signal.release is None


async def test_ingest_is_idempotent_on_message_id(db_session):
    notif = _a_fixture_notification()
    await ingest_sns_notification(db_session, notif, _NOW)
    await ingest_sns_notification(db_session, notif, _NOW)  # duplicate delivery
    count = (
        await db_session.execute(select(func.count()).select_from(Signal))
    ).scalar_one()
    assert count == 1  # unique dedup_key -> no second row


# --- the SubscriptionConfirmation handshake ---------------------------------


async def test_confirm_subscription_gets_allowlisted_url_and_records_a_job(db_session):
    hits = []

    def handler(request: httpx.Request) -> httpx.Response:
        hits.append(str(request.url))
        return httpx.Response(200, text="<ConfirmSubscriptionResponse/>")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    message = {
        "Type": "SubscriptionConfirmation",
        "MessageId": "m-1",
        "TopicArn": "arn:aws:sns:us-east-1:123456789012:tcf-prod-alarms",
        "SubscribeURL": "https://sns.us-east-1.amazonaws.com/?Action=ConfirmSubscription&Token=xyz",
    }
    try:
        job = await confirm_subscription(db_session, message, client=client)
    finally:
        await client.aclose()
    assert hits and hits[0].startswith("https://sns.us-east-1.amazonaws.com")
    assert job.type == "sns_subscription_confirmation"  # audit trail, not a Signal


async def test_confirm_subscription_rejects_disallowed_subscribe_url(db_session):
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200))
    )
    message = {
        "Type": "SubscriptionConfirmation",
        "MessageId": "m-2",
        "TopicArn": "arn:aws:sns:us-east-1:123456789012:tcf-prod-alarms",
        "SubscribeURL": "https://evil.example.com/?Action=ConfirmSubscription",
    }
    try:
        with pytest.raises(ValueError):
            await confirm_subscription(db_session, message, client=client)
    finally:
        await client.aclose()


# --- the route ---------------------------------------------------------------


@pytest.fixture
def sns_cert(monkeypatch):
    """Point the SNS verifier at the vendored cert for the duration of a test."""
    monkeypatch.setenv("SNS_SIGNING_CERT_PATH", str(CERT_PATH))
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _fixture_envelope():
    env = json.loads(sorted(SNS_DIR.glob("*.json"))[0].read_text())
    return env, env["raw_body"].encode("latin-1")


async def test_route_rejects_missing_message_type_header(client, sns_cert):
    _, body = _fixture_envelope()
    resp = await client.post("/ingest/sns", content=body)
    assert resp.status_code == 400


async def test_route_rejects_a_tampered_body(client, sns_cert):
    env, _ = _fixture_envelope()
    tampered = env["raw_body"].replace("ALARM", "OK").encode("latin-1")
    resp = await client.post(
        "/ingest/sns",
        content=tampered,
        headers={"x-amz-sns-message-type": "Notification"},
    )
    assert resp.status_code == 401


async def test_route_ingests_a_signed_notification(client, sns_cert):
    env, body = _fixture_envelope()
    resp = await client.post(
        "/ingest/sns",
        content=body,
        headers={"x-amz-sns-message-type": "Notification"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["signal_id"] and data["incident_id"]


# --- resolution auto-detect: ALARM -> OK clears the incident (M4 Task 2) -----


def test_alarm_state_reads_new_state_value():
    assert alarm_state(_alarm_notification("x", "OK", "m")) == "OK"
    assert alarm_state(_alarm_notification("x", "ALARM", "m")) == "ALARM"
    assert alarm_state({"Message": "not json"}) is None


async def test_alarm_ok_resolves_the_open_incident(db_session):
    alarm_name = "tcf-prod-elasticache-health"
    sig = await ingest_sns_notification(
        db_session, _alarm_notification(alarm_name, "ALARM", "m-alarm"), _NOW
    )
    incident = await correlate_signal(db_session, sig, 600)
    assert incident.status == "open"

    resolved = await resolve_from_alarm_ok(
        db_session,
        _alarm_notification(alarm_name, "OK", "m-ok"),
        _NOW + timedelta(minutes=5),
    )
    assert resolved is not None and resolved.id == incident.id
    await db_session.refresh(incident)
    assert incident.status == "resolved"
    assert incident.resolution_source == "sns_ok"


async def test_alarm_ok_with_no_matching_incident_is_a_noop(db_session):
    resolved = await resolve_from_alarm_ok(
        db_session, _alarm_notification("tcf-prod-unknown", "OK", "m-ok2"), _NOW
    )
    assert resolved is None


async def test_route_ok_transition_resolves_not_reopens(
    client, db_session, monkeypatch
):
    """An OK notification through the route resolves the incident (and opens none)."""
    monkeypatch.setenv("SNS_SIGNATURE_STRICT", "false")  # unsigned dev POST
    get_settings.cache_clear()
    try:
        alarm_name = "tcf-prod-rds-connections"
        sig = await ingest_sns_notification(
            db_session, _alarm_notification(alarm_name, "ALARM", "m-a"), _NOW
        )
        incident = await correlate_signal(db_session, sig, 600)
        await db_session.commit()

        ok = _alarm_notification(alarm_name, "OK", "m-o")
        resp = await client.post(
            "/ingest/sns",
            content=json.dumps(ok).encode(),
            headers={"x-amz-sns-message-type": "Notification"},
        )
        assert resp.status_code == 200
        assert resp.json()["resolved_incident_id"] == incident.id
        assert (
            await db_session.execute(select(func.count()).select_from(Incident))
        ).scalar_one() == 1  # resolved the one incident, opened no second
    finally:
        get_settings.cache_clear()


# --- cross-source dedup with the real redis-down fixtures (the PRD metric) ----


async def test_redis_down_sentry_plus_sns_yields_exactly_one_incident(db_session):
    """The PRD dedup metric, now cross-source: redis-down replayed with BOTH its
    Sentry fixtures AND its SNS alarm fixture must yield exactly 1 incident."""
    run = next(r for r in load_all_run_records() if r.fault_id == "redis-down")
    assert run.sns, "redis-down should carry an SNS fixture"

    # Sentry first (opens the incident), then the SNS alarm (joins it).
    for rel in run.fixture_paths:
        if "sentry" not in rel:
            continue
        env = json.loads((REPO_ROOT / rel).read_text())
        signal = await ingest_sentry(
            db_session,
            env["raw_body"].encode("latin-1"),
            datetime.fromisoformat(env["received_at"]),
        )
        await correlate_signal(db_session, signal, 600)

    sns_env = json.loads((REPO_ROOT / run.sns).read_text())
    notif = json.loads(sns_env["raw_body"].encode("latin-1"))
    alarm_signal = await ingest_sns_notification(
        db_session, notif, datetime.fromisoformat(sns_env["received_at"])
    )
    await correlate_signal(db_session, alarm_signal, 600)

    count = (
        await db_session.execute(select(func.count()).select_from(Incident))
    ).scalar_one()
    assert count == 1  # one outage, one brief — cross-source
