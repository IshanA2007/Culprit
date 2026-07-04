"""HMAC signature verification over the raw request body.

Mirrors the M1 verification (tests/test_corpus.py, harness/scrub.py): the secret
is env-injected, the digest is computed over the exact received bytes, and the
comparison is constant-time. Never trust an unsigned request (plan decision 3).

* Sentry  — header ``Sentry-Hook-Signature`` = ``hexdigest`` (no prefix),
            secret = ``SENTRY_CLIENT_SECRET``.
* GitHub   — header ``X-Hub-Signature-256`` = ``sha256=<hexdigest>``,
            secret = ``CULPRIT_GH_WEBHOOK_SECRET``.
"""

from __future__ import annotations

import hashlib
import hmac


def verify_sentry(secret: str | None, body: bytes, signature: str | None) -> bool:
    """True iff ``signature`` is a valid Sentry HMAC-SHA256 hexdigest of ``body``."""
    if not secret or not signature:
        return False
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


def verify_github(secret: str | None, body: bytes, signature: str | None) -> bool:
    """True iff ``signature`` is a valid ``sha256=<hexdigest>`` of ``body``."""
    if not secret or not signature or not signature.startswith("sha256="):
        return False
    expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)
