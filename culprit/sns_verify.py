"""SNS signature verification + SSRF-guarded cert/handshake fetches (decision 5).

Genuine SNS verification: reconstruct the canonical string per ``SignatureVersion``
(1 = RSA-SHA1, 2 = RSA-SHA256), verify the base64 ``Signature`` against the X.509
public key. The signing cert is fetched from ``SigningCertURL`` — a remote fetch
Culprit performs, so it is an SSRF surface: both ``SigningCertURL`` and the
handshake ``SubscribeURL`` are constrained to **https + ``sns.<region>.amazonaws.com``**
(a strict host allowlist). Offline/fixture mode injects a vendored cert instead of
fetching (the synthesized fixtures are signed by the vendored keypair).
"""

from __future__ import annotations

import base64
from urllib.parse import urlparse

import httpx
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.x509 import load_pem_x509_certificate

# Canonical field order per the AWS SNS message signing spec.
_NOTIFICATION_FIELDS = (
    "Message",
    "MessageId",
    "Subject",
    "Timestamp",
    "TopicArn",
    "Type",
)
_SUBSCRIPTION_FIELDS = (
    "Message",
    "MessageId",
    "SubscribeURL",
    "Timestamp",
    "Token",
    "TopicArn",
    "Type",
)


def is_allowed_sns_url(url: str | None) -> bool:
    """True iff ``url`` is https and its host is ``sns.<...>.amazonaws.com``.

    The SSRF guard for both ``SigningCertURL`` and ``SubscribeURL``: rejects any
    non-https scheme, any non-amazonaws host, suffix-smuggling
    (``...amazonaws.com.attacker.com``), and non-``sns`` amazonaws hosts (``s3``…).
    """
    if not url:
        return False
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    return (
        parsed.scheme == "https"
        and host.startswith("sns.")
        and host.endswith(".amazonaws.com")
    )


def canonical_message(message: dict) -> str:
    """Reconstruct the SNS signing string for a message (order per its Type)."""
    fields = (
        _NOTIFICATION_FIELDS
        if message.get("Type") == "Notification"
        else _SUBSCRIPTION_FIELDS
    )
    parts = []
    for field in fields:
        # Subject is optional; only signed when present.
        if field == "Subject" and message.get("Subject") is None:
            continue
        if field in message:
            parts.append(f"{field}\n{message[field]}\n")
    return "".join(parts)


def verify_signature(message: dict, cert_pem: bytes) -> bool:
    """Verify a message's ``Signature`` against a PEM X.509 cert."""
    try:
        cert = load_pem_x509_certificate(cert_pem)
        algo = (
            hashes.SHA1() if message.get("SignatureVersion") == "1" else hashes.SHA256()
        )
        cert.public_key().verify(
            base64.b64decode(message["Signature"]),
            canonical_message(message).encode(),
            padding.PKCS1v15(),
            algo,
        )
        return True
    except (InvalidSignature, KeyError, ValueError):
        return False


async def fetch_cert(url: str, *, client: httpx.AsyncClient) -> bytes:
    """Fetch a signing cert from an allowlisted ``SigningCertURL`` (SSRF-guarded)."""
    if not is_allowed_sns_url(url):
        raise ValueError(f"disallowed SigningCertURL host: {url!r}")
    resp = await client.get(url)
    resp.raise_for_status()
    return resp.content


async def verify_message(
    message: dict,
    *,
    cert_pem: bytes | None = None,
    client: httpx.AsyncClient | None = None,
) -> bool:
    """Verify an SNS message, using an injected cert or fetching (allowlisted).

    ``cert_pem`` given -> offline verification (fixtures/dev). Otherwise the cert is
    fetched from ``SigningCertURL`` under the host allowlist (live)."""
    if cert_pem is None:
        url = message.get("SigningCertURL")
        if not is_allowed_sns_url(url):
            return False
        owns = client is None
        client = client or httpx.AsyncClient(timeout=10.0)
        try:
            cert_pem = await fetch_cert(url, client=client)
        finally:
            if owns:
                await client.aclose()
    return verify_signature(message, cert_pem)
