"""Synthesized SNS/CloudWatch feed generator — the silent-fault ingest contract.

The silent faults (N+1, cartesian join, dropped index, zero-results) and the
gunicorn-OOM emit **no Sentry event** — they surface as latency/5xx/behavior and
are caught by CloudWatch alarms delivered over an SNS HTTPS subscription. tCF has
zero alarms today, so there is nothing to record live; this module reconstructs
those deliveries **shape-faithfully from real data**, exactly as
``harness/deployfeed.py`` did for the GitHub deploy feed:

* the envelope *schema* is a real SNS HTTPS ``Notification`` whose ``Message`` is a
  real CloudWatch alarm state-change (``snsfeed_inputs/template_notification.json``,
  from AWS's published message formats) — parity enforced by ``test_snsfeed.py``;
* the alarm ``Message`` *values* (AlarmName, metric, dimensions, threshold) come
  from ``docs/aws/alarms-proposal.tf`` (the alarm suite Culprit proposes) via the
  fault -> alarm map below — an alarm name is a generic infra metric, never a fault
  id (anti-leakage, the ``head_branch: master`` precedent);
* only opaque ids (MessageId, subscription arn, cert hash) and the alarm timestamp
  are synthesized — deterministically, so regeneration is byte-stable;
* signatures are **real**: RSA over the SNS canonical string with a locally
  generated keypair whose cert is vendored (``snsfeed_inputs/sns_signing_cert.pem``,
  committed) so the verification code path runs offline. The private key is
  env/file-local and gitignored (the deployfeed webhook-secret posture); the
  documented delta is that the cert is ours, not Amazon's (live mode pins the
  amazonaws.com allowlist — ``culprit/sns_verify.py``).

Everything is offline + deterministic (no ``Date.now``/random). Timestamps are the
run's ``injected_at`` plus a documented alarm-evaluation delay.
"""

from __future__ import annotations

import base64
import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.x509 import load_pem_x509_certificate

from harness.config import FIXTURES_DIR, REPO_ROOT
from harness.runrecord import RunRecord, load_all_run_records

INPUTS_DIR = REPO_ROOT / "harness" / "snsfeed_inputs"
SNS_FIXTURE_DIR = FIXTURES_DIR / "sns"
SIGNING_KEY_PATH = INPUTS_DIR / "sns_signing_key.pem"
SIGNING_CERT_PATH = INPUTS_DIR / "sns_signing_cert.pem"

# Generic, fault-agnostic topic + account (anti-leakage — no fault identity).
AWS_REGION = "us-east-1"
AWS_ACCOUNT = "123456789012"
TOPIC_NAME = "tcf-prod-alarms"
TOPIC_ARN = f"arn:aws:sns:{AWS_REGION}:{AWS_ACCOUNT}:{TOPIC_NAME}"
# CloudWatch fires after evaluation_periods x period; documented alarm delay.
ALARM_DELAY_SECONDS = 180
SIGNATURE_VERSION = "1"  # SNS SignatureVersion 1 = RSA-SHA1 over the canonical string


@dataclass(frozen=True)
class AlarmSpec:
    """One proposed CloudWatch alarm (mirrors docs/aws/alarms-proposal.tf)."""

    alarm_name: str
    description: str
    namespace: str
    metric_name: str
    statistic: str
    dimensions: tuple[tuple[str, str], ...]  # (name, value)
    comparison_operator: str
    threshold: float
    period: int
    evaluation_periods: int


# The alarm suite — names/metrics/dimensions are the source of truth shared with
# docs/aws/alarms-proposal.tf. Fault-agnostic by construction.
ALARMS: dict[str, AlarmSpec] = {
    "alb-target-response-time": AlarmSpec(
        alarm_name="tcf-prod-alb-target-response-time",
        description="ALB p95 target response time is elevated (latency regression).",
        namespace="AWS/ApplicationELB",
        metric_name="TargetResponseTime",
        statistic="P95",
        dimensions=(("LoadBalancer", "app/tcf-prod-alb/50dc6c495c0c9188"),),
        comparison_operator="GreaterThanThreshold",
        threshold=2.0,
        period=60,
        evaluation_periods=3,
    ),
    "alb-5xx": AlarmSpec(
        alarm_name="tcf-prod-alb-5xx",
        description="Elevated ELB/target 5xx responses from the ALB.",
        namespace="AWS/ApplicationELB",
        metric_name="HTTPCode_ELB_5XX_Count",
        statistic="Sum",
        dimensions=(("LoadBalancer", "app/tcf-prod-alb/50dc6c495c0c9188"),),
        comparison_operator="GreaterThanThreshold",
        threshold=5.0,
        period=60,
        evaluation_periods=2,
    ),
    "elasticache-health": AlarmSpec(
        alarm_name="tcf-prod-elasticache-health",
        description="ElastiCache node unreachable / connection failures.",
        namespace="AWS/ElastiCache",
        metric_name="CurrConnections",
        statistic="Minimum",
        dimensions=(("CacheClusterId", "tcf-prod-redis-001"),),
        comparison_operator="LessThanThreshold",
        threshold=1.0,
        period=60,
        evaluation_periods=2,
    ),
    "rds-connections": AlarmSpec(
        alarm_name="tcf-prod-rds-connections",
        description="RDS database connections near the db.t3.micro ceiling.",
        namespace="AWS/RDS",
        metric_name="DatabaseConnections",
        statistic="Maximum",
        dimensions=(("DBInstanceIdentifier", "tcf-prod-db"),),
        comparison_operator="GreaterThanThreshold",
        threshold=80.0,
        period=60,
        evaluation_periods=2,
    ),
    "search-canary": AlarmSpec(
        alarm_name="tcf-prod-search-canary",
        description="Search-smoke synthetic canary: a known-good query returned zero results.",
        namespace="CloudWatchSynthetics",
        metric_name="SuccessPercent",
        statistic="Average",
        dimensions=(("CanaryName", "tcf-prod-search-smoke"),),
        comparison_operator="LessThanThreshold",
        threshold=100.0,
        period=300,
        evaluation_periods=1,
    ),
}

# fault_id -> alarm key. Only silent faults + the infra dedup cases (plan Corpus
# & eval deltas table). Sentry-visible code faults do NOT get an SNS fixture.
FAULT_ALARM: dict[str, str] = {
    "n-plus-one-section-instructor-prefetch": "alb-target-response-time",
    "cartesian-join-gpa-annotation-timeout": "alb-target-response-time",
    "bad-migration-drop-trigram-gin-indexes": "alb-target-response-time",
    "search-silent-zero-results": "search-canary",
    "gunicorn-worker-oom": "alb-5xx",
    "redis-down": "elasticache-health",
    "db-stopped": "rds-connections",
}


# --- deterministic synthesis (no Date.now / random) -------------------------


def _digest(*parts: str) -> bytes:
    return hashlib.sha256("|".join(parts).encode()).digest()


def _message_id(run_id: str) -> str:
    return str(uuid.UUID(bytes=_digest(run_id, "sns-message")[:16]))


def _subscription_arn(run_id: str) -> str:
    sub = str(uuid.UUID(bytes=_digest(run_id, "sns-subscription")[:16]))
    return f"{TOPIC_ARN}:{sub}"


def _cert_hash(run_id: str) -> str:
    return _digest(run_id, "sns-cert").hex()[:32]


def _iso_from_compact(ts: str) -> datetime:
    return datetime.strptime(ts, "%Y%m%dT%H%M%S")


def _alarm_time(run: RunRecord) -> datetime:
    return _iso_from_compact(run.injected_at) + timedelta(seconds=ALARM_DELAY_SECONDS)


def _sns_timestamp(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _cw_timestamp(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S.000+0000")


# --- payload assembly -------------------------------------------------------


def build_alarm_message(run: RunRecord, alarm: AlarmSpec) -> dict:
    """The CloudWatch alarm state-change JSON (the SNS ``Message`` body)."""
    changed = _alarm_time(run)
    reason = (
        f"Threshold Crossed: {alarm.evaluation_periods} datapoint(s) "
        f"{alarm.comparison_operator} the threshold ({alarm.threshold})."
    )
    return {
        "AlarmName": alarm.alarm_name,
        "AlarmDescription": alarm.description,
        "AWSAccountId": AWS_ACCOUNT,
        "AlarmConfigurationUpdatedTimestamp": "2026-07-01T00:00:00.000+0000",
        "NewStateValue": "ALARM",
        "NewStateReason": reason,
        "StateChangeTime": _cw_timestamp(changed),
        "Region": "US East (N. Virginia)",
        "AlarmArn": (
            f"arn:aws:cloudwatch:{AWS_REGION}:{AWS_ACCOUNT}:alarm:{alarm.alarm_name}"
        ),
        "OldStateValue": "OK",
        "OKActions": [TOPIC_ARN],
        "AlarmActions": [TOPIC_ARN],
        "InsufficientDataActions": [],
        "Trigger": {
            "MetricName": alarm.metric_name,
            "Namespace": alarm.namespace,
            "StatisticType": "Statistic",
            "Statistic": alarm.statistic,
            "Unit": None,
            "Dimensions": [
                {"value": value, "name": name} for name, value in alarm.dimensions
            ],
            "Period": alarm.period,
            "EvaluationPeriods": alarm.evaluation_periods,
            "ComparisonOperator": alarm.comparison_operator,
            "Threshold": alarm.threshold,
            "TreatMissingData": "missing",
            "EvaluateLowSampleCountPercentile": "",
        },
    }


def canonical_message(notification: dict) -> str:
    """The SNS signing string for a Notification (field order per the AWS spec)."""
    fields = ["Message", "MessageId"]
    if notification.get("Subject") is not None:
        fields.append("Subject")
    fields += ["Timestamp", "TopicArn", "Type"]
    return "".join(f"{f}\n{notification[f]}\n" for f in fields)


def _sign(canonical: str, key_pem: bytes) -> str:
    key = serialization.load_pem_private_key(key_pem, password=None)
    algo = hashes.SHA1() if SIGNATURE_VERSION == "1" else hashes.SHA256()
    sig = key.sign(canonical.encode(), padding.PKCS1v15(), algo)
    return base64.b64encode(sig).decode()


def verify_signature(notification: dict, cert_pem: bytes) -> bool:
    """Verify a Notification's Signature against a PEM cert (offline path)."""
    cert = load_pem_x509_certificate(cert_pem)
    algo = hashes.SHA1() if notification["SignatureVersion"] == "1" else hashes.SHA256()
    try:
        cert.public_key().verify(
            base64.b64decode(notification["Signature"]),
            canonical_message(notification).encode(),
            padding.PKCS1v15(),
            algo,
        )
        return True
    except InvalidSignature:
        return False


def build_notification(
    run: RunRecord, alarm: AlarmSpec, *, key_pem: bytes | None = None
) -> dict:
    """The SNS HTTPS ``Notification`` envelope wrapping the alarm Message."""
    message = json.dumps(build_alarm_message(run, alarm))
    message_id = _message_id(run.run_id)
    cert_url = (
        f"https://sns.{AWS_REGION}.amazonaws.com/"
        f"SimpleNotificationService-{_cert_hash(run.run_id)}.pem"
    )
    unsubscribe = (
        f"https://sns.{AWS_REGION}.amazonaws.com/?Action=Unsubscribe"
        f"&SubscriptionArn={_subscription_arn(run.run_id)}"
    )
    notif = {
        "Type": "Notification",
        "MessageId": message_id,
        "TopicArn": TOPIC_ARN,
        "Subject": f'ALARM: "{alarm.alarm_name}" in US East (N. Virginia)',
        "Message": message,
        "Timestamp": _sns_timestamp(_alarm_time(run)),
        "SignatureVersion": SIGNATURE_VERSION,
        "Signature": "",
        "SigningCertURL": cert_url,
        "UnsubscribeURL": unsubscribe,
    }
    if key_pem:
        notif["Signature"] = _sign(canonical_message(notif), key_pem)
    return notif


def sns_fixture_name(run: RunRecord) -> str:
    """Deterministic ``<injected_at>-<8hex>.json`` (mirrors the recorder naming)."""
    suffix = _digest(run.run_id, "sns-fixture").hex()[:8]
    return f"{run.injected_at}-{suffix}.json"


def build_envelope(
    run: RunRecord, alarm: AlarmSpec, *, key_pem: bytes | None = None
) -> dict:
    """Wrap the Notification in the recorder envelope (byte-for-byte ingest shape).

    Content-Type is ``text/plain`` (the SNS gotcha): the body is JSON but the
    route must dispatch on ``x-amz-sns-message-type``, not the content type."""
    notif = build_notification(run, alarm, key_pem=key_pem)
    body_bytes = json.dumps(notif, ensure_ascii=False, separators=(",", ":")).encode(
        "utf-8"
    )
    raw_body = body_bytes.decode("latin-1")
    return {
        "received_at": _sns_timestamp(_alarm_time(run)),
        "source": "sns",
        "resource": "notification",
        "headers": {
            "content-type": "text/plain; charset=UTF-8",
            "user-agent": "Amazon Simple Notification Service Agent",
            "x-amz-sns-message-type": "Notification",
            "x-amz-sns-message-id": notif["MessageId"],
            "x-amz-sns-topic-arn": TOPIC_ARN,
            "x-amz-sns-subscription-arn": _subscription_arn(run.run_id),
        },
        "raw_body": raw_body,
        "reconstructed": True,
    }


# --- vendored inputs + backfill ---------------------------------------------


def load_template(inputs_dir: Path | None = None) -> dict:
    """The vendored real SNS Notification + CloudWatch alarm schema template."""
    inputs_dir = inputs_dir or INPUTS_DIR
    return json.loads((inputs_dir / "template_notification.json").read_text())


def _load_signing_key(key_path: Path | None = None) -> bytes | None:
    key_path = key_path or SIGNING_KEY_PATH
    return key_path.read_bytes() if key_path.exists() else None


def write_sns_fixture(
    run: RunRecord,
    alarm: AlarmSpec,
    *,
    out_dir: Path | None = None,
    key_pem: bytes | None = None,
) -> Path:
    out_dir = out_dir or SNS_FIXTURE_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    env = build_envelope(run, alarm, key_pem=key_pem)
    dest = out_dir / sns_fixture_name(run)
    dest.write_text(json.dumps(env, indent=2))
    return dest


def backfill_sns(
    *,
    key_path: Path | None = None,
    out_dir: Path | None = None,
    runs_dir: Path | None = None,
) -> dict:
    """Generate an SNS fixture per silent/infra-dedup run and link it in the record.

    Deterministic + idempotent: re-running rewrites the same bytes and the same
    ``sns`` path. Signs with the vendored keypair when present (else unsigned)."""
    key_pem = _load_signing_key(key_path)
    runs = load_all_run_records(runs_dir)
    written = 0
    for run in runs:
        alarm_key = FAULT_ALARM.get(run.fault_id)
        if alarm_key is None:
            continue
        dest = write_sns_fixture(
            run, ALARMS[alarm_key], out_dir=out_dir, key_pem=key_pem
        )
        run.sns = str(dest.relative_to(REPO_ROOT))
        run.write(runs_dir)
        written += 1
    return {
        "runs": len(runs),
        "fixtures_written": written,
        "signed": key_pem is not None,
    }
