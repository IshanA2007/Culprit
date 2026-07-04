"""Unit tests for the synthesized SNS feed generator (``harness/snsfeed.py``).

Mirrors ``tests/test_deployfeed.py``: the SNS fixtures reconstruct shape-faithful
CloudWatch-alarm-over-SNS HTTPS deliveries for the silent-fault + infra-dedup
runs. The envelope *schema* comes from a vendored real SNS message format; the
alarm ``Message`` values come from the alarms proposal. Only opaque ids/timestamps
are synthesized (deterministically). Signatures are real (RSA over the SNS
canonical string) — here against an inline test keypair so the roundtrip runs in
CI; the committed fixtures use the vendored keypair.
"""

from __future__ import annotations

import json

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from harness import snsfeed
from harness.runrecord import RunRecord, WindowCommit

# --- an inline throwaway keypair for the sign/verify roundtrip ---------------

_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_KEY_PEM = _KEY.private_bytes(
    serialization.Encoding.PEM,
    serialization.PrivateFormat.PKCS8,
    serialization.NoEncryption(),
)


def _cert_pem() -> bytes:
    import datetime as dt

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes
    from cryptography.x509.oid import NameOID

    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "test")])
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(_KEY.public_key())
        .serial_number(1)
        .not_valid_before(dt.datetime(2026, 1, 1, tzinfo=dt.UTC))
        .not_valid_after(dt.datetime(2036, 1, 1, tzinfo=dt.UTC))
        .sign(_KEY, hashes.SHA256())
    )
    return cert.public_bytes(serialization.Encoding.PEM)


def _run(fault_id="n-plus-one-section-instructor-prefetch", **overrides) -> RunRecord:
    base = dict(
        run_id=f"20260704T035806-{fault_id}-w1",
        fault_id=fault_id,
        fault_class="code",
        ground_truth="culprit_commit",
        base_sha="1" * 40,
        release_sha="2126ec08b659479e2231601ccf2683e5a034a222",
        window=[WindowCommit(sha="e0a0" + "0" * 36, message="x", is_culprit=True)],
        culprit_sha="e0a0" + "0" * 36,
        injected_at="20260704T035806",
    )
    base.update(overrides)
    return RunRecord(**base)


# --- the fault -> alarm map --------------------------------------------------


def test_fault_alarm_map_points_at_real_alarm_specs():
    assert snsfeed.FAULT_ALARM, "no fault->alarm mapping"
    for fault_id, alarm_key in snsfeed.FAULT_ALARM.items():
        assert alarm_key in snsfeed.ALARMS, f"{fault_id} -> missing alarm {alarm_key}"


def test_map_covers_the_silent_and_infra_dedup_faults():
    expected = {
        "n-plus-one-section-instructor-prefetch",
        "cartesian-join-gpa-annotation-timeout",
        "bad-migration-drop-trigram-gin-indexes",
        "search-silent-zero-results",
        "gunicorn-worker-oom",
        "redis-down",
        "db-stopped",
    }
    assert set(snsfeed.FAULT_ALARM) == expected


# --- schema faithfulness vs the vendored template ----------------------------


def test_notification_keys_match_vendored_template():
    template = snsfeed.load_template()["notification"]
    notif = snsfeed.build_notification(
        _run(), snsfeed.ALARMS["alb-target-response-time"]
    )
    assert set(notif) == set(template), (
        f"drift: extra={set(notif) - set(template)}, "
        f"missing={set(template) - set(notif)}"
    )


def test_alarm_message_keys_match_vendored_template():
    template = snsfeed.load_template()["message"]
    msg = snsfeed.build_alarm_message(
        _run(), snsfeed.ALARMS["alb-target-response-time"]
    )
    assert set(msg) == set(template)
    assert set(msg["Trigger"]) == set(template["Trigger"])


# --- CloudWatch alarm content ------------------------------------------------


def test_alarm_message_is_an_alarm_state_change():
    msg = snsfeed.build_alarm_message(_run(), snsfeed.ALARMS["alb-5xx"])
    assert msg["NewStateValue"] == "ALARM"
    assert msg["OldStateValue"] == "OK"
    assert msg["Trigger"]["MetricName"] == "HTTPCode_ELB_5XX_Count"
    assert msg["Trigger"]["Namespace"] == "AWS/ApplicationELB"


# --- the SNS envelope + the text/plain gotcha --------------------------------


def test_envelope_is_recorder_shaped():
    env = snsfeed.build_envelope(_run(), snsfeed.ALARMS["alb-target-response-time"])
    assert env["source"] == "sns"
    assert env["resource"] == "notification"
    assert env.get("reconstructed") is True
    body = json.loads(env["raw_body"].encode("latin-1"))
    assert body["Type"] == "Notification"


def test_content_type_is_text_plain_and_type_header_routes():
    # The classic gotcha: SNS delivers JSON with Content-Type text/plain, so the
    # route must dispatch on x-amz-sns-message-type, not the content type.
    env = snsfeed.build_envelope(_run(), snsfeed.ALARMS["alb-5xx"])
    assert env["headers"]["content-type"].startswith("text/plain")
    assert env["headers"]["x-amz-sns-message-type"] == "Notification"


# --- determinism (byte-stable regeneration, plan requirement) ----------------


def test_notification_is_deterministic():
    a = snsfeed.build_notification(_run(), snsfeed.ALARMS["alb-5xx"])
    b = snsfeed.build_notification(_run(), snsfeed.ALARMS["alb-5xx"])
    assert json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)


def test_fixture_name_is_deterministic_and_timestamped():
    name = snsfeed.sns_fixture_name(_run())
    assert name.endswith(".json") and name.startswith("20260704T035806")
    assert name == snsfeed.sns_fixture_name(_run())


# --- anti-leakage: the payload must never name the fault ---------------------


def test_payload_never_names_the_fault():
    for fault_id in snsfeed.FAULT_ALARM:
        env = snsfeed.build_envelope(
            _run(fault_id=fault_id), snsfeed.ALARMS[snsfeed.FAULT_ALARM[fault_id]]
        )
        blob = json.dumps(env)
        assert fault_id not in blob, f"{fault_id} leaks into its SNS payload"
        # alarm names are generic infra metric names, never a fault id
        body = json.loads(env["raw_body"].encode("latin-1"))
        message = json.loads(body["Message"])
        assert fault_id not in message["AlarmName"]


# --- real signature over the SNS canonical string ----------------------------


def test_sign_and_verify_roundtrip():
    notif = snsfeed.build_notification(
        _run(), snsfeed.ALARMS["elasticache-health"], key_pem=_KEY_PEM
    )
    assert notif["Signature"]
    assert snsfeed.verify_signature(notif, _cert_pem()) is True


def test_tampered_message_fails_verification():
    notif = snsfeed.build_notification(
        _run(), snsfeed.ALARMS["elasticache-health"], key_pem=_KEY_PEM
    )
    notif["Message"] = notif["Message"].replace("ALARM", "OK")  # tamper
    assert snsfeed.verify_signature(notif, _cert_pem()) is False


def test_signature_version_is_set():
    notif = snsfeed.build_notification(
        _run(), snsfeed.ALARMS["alb-5xx"], key_pem=_KEY_PEM
    )
    assert notif["SignatureVersion"] in ("1", "2")
