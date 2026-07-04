"""FastAPI application — the ingest surface.

Task 1 ships ``/health`` only. Tasks 3–4 add ``POST /ingest/sentry`` and
``POST /ingest/github`` (verify signature -> parse -> persist -> maybe open an
incident). The recorded webhook envelope (``harness/recorder/app.py``) is the
seed of this contract: live endpoints receive the same raw bytes the recorder
captured, verify the HMAC, then parse.
"""

from __future__ import annotations

from fastapi import FastAPI

app = FastAPI(title="Culprit")


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}
