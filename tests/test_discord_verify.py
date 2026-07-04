"""Task 2 — Discord interaction verification (Ed25519 over timestamp + body).

Discord signs every interaction with the app's Ed25519 key: the signature covers
``timestamp + raw_body``, verified with the app public key. Same "never trust an
unsigned request" stance as the SNS X.509 boundary — a bad or missing signature is
rejected (the route turns that into a 401).
"""

from __future__ import annotations

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from culprit.discord_verify import verify_interaction


def _keypair() -> tuple[Ed25519PrivateKey, str]:
    priv = Ed25519PrivateKey.generate()
    return priv, priv.public_key().public_bytes_raw().hex()


def test_valid_signature_verifies():
    priv, pub_hex = _keypair()
    ts, body = "1700000000", b'{"type":1}'
    sig = priv.sign(ts.encode() + body).hex()
    assert verify_interaction(pub_hex, ts, body, sig) is True


def test_tampered_body_fails():
    priv, pub_hex = _keypair()
    ts, body = "1700000000", b'{"type":1}'
    sig = priv.sign(ts.encode() + body).hex()
    assert verify_interaction(pub_hex, ts, b'{"type":2}', sig) is False


def test_garbage_signature_fails():
    _, pub_hex = _keypair()
    assert verify_interaction(pub_hex, "1", b"x", "notahexsignature") is False


def test_missing_public_key_is_rejected():
    assert verify_interaction(None, "1", b"x", "00" * 64) is False


def test_missing_signature_or_timestamp_is_rejected():
    _, pub_hex = _keypair()
    assert verify_interaction(pub_hex, None, b"x", "00") is False
    assert verify_interaction(pub_hex, "1", b"x", None) is False
