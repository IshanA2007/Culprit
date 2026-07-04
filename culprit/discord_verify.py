"""Ed25519 verification for Discord interactions.

Discord signs every interaction webhook with the application's Ed25519 key: the
signature (header ``X-Signature-Ed25519``) covers ``timestamp + raw_body`` (the
timestamp comes from ``X-Signature-Timestamp``), verified with the app's public
key. This is the Discord analog of the SNS X.509 boundary (``culprit/sns_verify``)
— the endpoint rejects anything that does not verify (401). Pure and offline: the
public key is a config value, no network fetch, so there is no SSRF surface here.
"""

from __future__ import annotations

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey


def verify_interaction(
    public_key_hex: str | None,
    timestamp: str | None,
    body: bytes,
    signature_hex: str | None,
) -> bool:
    """True iff ``signature_hex`` is a valid Ed25519 signature over ts+body.

    Missing key/signature/timestamp -> False (an unconfigured endpoint verifies
    nothing, so it is inert-and-secure by default).
    """
    if not public_key_hex or not signature_hex or timestamp is None:
        return False
    try:
        pub = Ed25519PublicKey.from_public_bytes(bytes.fromhex(public_key_hex))
        pub.verify(bytes.fromhex(signature_hex), timestamp.encode() + body)
        return True
    except (InvalidSignature, ValueError):
        return False
