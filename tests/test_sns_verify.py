"""Task 7 — SNS signature verification + SSRF host allowlist (plan decision 5).

``culprit/sns_verify.py`` verifies genuine SNS signatures (X.509 over the SNS
canonical string) and guards the ``SigningCertURL`` / ``SubscribeURL`` fetches with
a strict https + ``sns.<region>.amazonaws.com`` allowlist. Here we verify the
committed fixtures against the vendored cert (offline) and pin the allowlist.
"""

from __future__ import annotations

import json

from culprit import sns_verify
from culprit.config import REPO_ROOT

CERT = (REPO_ROOT / "harness" / "snsfeed_inputs" / "sns_signing_cert.pem").read_bytes()
SNS_DIR = REPO_ROOT / "fixtures" / "sns"


def _a_notification() -> dict:
    env = json.loads(sorted(SNS_DIR.glob("*.json"))[0].read_text())
    return json.loads(env["raw_body"].encode("latin-1"))


def test_allowlist_accepts_https_sns_amazonaws():
    assert sns_verify.is_allowed_sns_url(
        "https://sns.us-east-1.amazonaws.com/SimpleNotificationService-abc.pem"
    )


def test_allowlist_rejects_non_amazonaws_host():
    assert not sns_verify.is_allowed_sns_url("https://evil.example.com/cert.pem")


def test_allowlist_rejects_http_scheme():
    assert not sns_verify.is_allowed_sns_url(
        "http://sns.us-east-1.amazonaws.com/cert.pem"
    )


def test_allowlist_rejects_suffix_smuggling():
    # sns.<...>.amazonaws.com.attacker.com must NOT pass
    assert not sns_verify.is_allowed_sns_url(
        "https://sns.us-east-1.amazonaws.com.attacker.com/cert.pem"
    )
    # a non-sns amazonaws host must NOT pass (only sns.* is allowed)
    assert not sns_verify.is_allowed_sns_url("https://s3.amazonaws.com/cert.pem")


def test_verify_a_real_fixture_signature():
    assert sns_verify.verify_signature(_a_notification(), CERT) is True


def test_tampered_notification_fails_verification():
    notif = _a_notification()
    notif["Message"] = notif["Message"].replace("ALARM", "OK")
    assert sns_verify.verify_signature(notif, CERT) is False


def test_canonical_string_orders_notification_fields():
    notif = _a_notification()
    canonical = sns_verify.canonical_message(notif)
    # AWS spec: Message first, Type last, name/value newline-separated
    assert canonical.startswith("Message\n")
    assert canonical.rstrip().endswith("Notification")
