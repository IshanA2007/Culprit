"""FastAPI catch-all webhook recorder.

Every inbound POST is dumped **raw** — body bytes untouched, headers preserved —
into ``fixtures/<source>/<ts>-<resource>.json``. It responds 200 in well under a
second (Sentry drops the webhook if we're slow) and never parses or transforms
the body, because the recorded bytes ARE the Milestone 2 ingest contract.

The catch-all path segment is the ``source`` (e.g. ``sentry``, ``github``), so a
single app records every provider:

    POST /sentry   -> fixtures/sentry/<ts>-<resource>.json
    POST /github   -> fixtures/github/<ts>-<resource>.json

Run it with:  uv run uvicorn harness.recorder.app:app --port 9000
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import UTC, datetime
from pathlib import Path

from fastapi import FastAPI, Request, Response

from harness.config import FIXTURES_DIR

app = FastAPI(title="Culprit webhook recorder")


def _fixtures_dir() -> Path:
    # Env override lets tests point at a tmp dir.
    override = os.environ.get("CULPRIT_FIXTURES_DIR")
    return Path(override) if override else FIXTURES_DIR


def _safe_segment(value: str) -> str:
    """Keep filenames tame without altering recorded content."""
    keep = "-_."
    cleaned = "".join(c if (c.isalnum() or c in keep) else "_" for c in value)
    return cleaned.strip("_") or "unknown"


def record_payload(source: str, headers: dict[str, str], raw_body: bytes) -> Path:
    """Write one raw payload to disk and return its path. Pure I/O, no parsing."""
    received_at = datetime.now(UTC).isoformat()
    # Sentry sends the resource type in a header; fall back to "event".
    resource = headers.get("sentry-hook-resource") or headers.get(
        "x-github-event", "event"
    )
    ts = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%f")
    dest_dir = _fixtures_dir() / _safe_segment(source)
    dest_dir.mkdir(parents=True, exist_ok=True)
    fname = f"{ts}-{_safe_segment(resource)}-{uuid.uuid4().hex[:8]}.json"
    dest = dest_dir / fname

    envelope = {
        "received_at": received_at,
        "source": source,
        "resource": resource,
        "headers": headers,
        # raw_body preserved byte-for-byte via latin-1 (1:1 byte<->codepoint),
        # so the exact wire bytes round-trip out of the JSON fixture.
        "raw_body": raw_body.decode("latin-1"),
    }
    dest.write_text(json.dumps(envelope, indent=2))
    return dest


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/{source:path}")
async def catch_all(source: str, request: Request) -> Response:
    raw_body = await request.body()
    headers = {k.lower(): v for k, v in request.headers.items()}
    path = record_payload(source or "root", headers, raw_body)
    # Respond immediately; the fixture path helps live debugging.
    return Response(
        content=json.dumps({"recorded": path.name}),
        media_type="application/json",
        status_code=200,
    )
