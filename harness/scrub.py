"""Scrub PII from recorded fixtures before they are committed.

The harness DB is a prod copy, so some fault events captured real
``@virginia.edu`` student emails in stack-frame locals / user context. Culprit
never uses emails for anything (culprit-matching = stack frames + the diff), so
we redact the email VALUES while preserving the exact payload shape — the
Milestone-2 ingest contract stays intact.

Two notes:
* Only emails are redacted. The IPs in the payloads are the harness traffic
  driver's own address (synthetic traffic), not student data.
* Editing the body invalidates Sentry's original HMAC, so we recompute the
  ``Sentry-Hook-Signature`` over the scrubbed body with the integration Client
  Secret. Signature verification (tests/test_corpus.py) still holds, and each
  scrubbed fixture is marked ``"scrubbed": true`` for transparency.

Run once before committing:  uv run culprit-harness scrub-fixtures
Idempotent — already-scrubbed fixtures are skipped.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re

from harness.config import FIXTURES_DIR

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
REDACTED = "redacted@example.com"


def scrub_all() -> dict:
    """Redact emails from every recorded Sentry fixture + re-sign. Returns stats."""
    secret = os.environ.get("SENTRY_CLIENT_SECRET", "")
    sentry_dir = FIXTURES_DIR / "sentry"
    scanned = scrubbed = emails_removed = 0
    for path in sorted(sentry_dir.rglob("*.json")):
        scanned += 1
        env = json.loads(path.read_text())
        if env.get("scrubbed"):
            continue

        body = env["raw_body"]
        hits = len(EMAIL_RE.findall(body))
        new_body = EMAIL_RE.sub(REDACTED, body)
        # headers are unlikely to hold emails, but redact defensively
        headers = env.get("headers", {})
        for k, v in list(headers.items()):
            if isinstance(v, str):
                headers[k] = EMAIL_RE.sub(REDACTED, v)

        env["raw_body"] = new_body
        # re-sign over the scrubbed body so verification still holds
        if secret and "sentry-hook-signature" in headers:
            headers["sentry-hook-signature"] = hmac.new(
                secret.encode(), new_body.encode("latin-1"), hashlib.sha256
            ).hexdigest()
        env["scrubbed"] = True
        path.write_text(json.dumps(env, indent=2))

        scrubbed += 1
        emails_removed += hits

    return {"scanned": scanned, "scrubbed": scrubbed, "emails_removed": emails_removed}
