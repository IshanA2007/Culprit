"""Recorder tests (plan Task 5) — the raw body is preserved byte-for-byte.

The recorded bytes ARE the Milestone 2 ingest contract, so the recorder must not
mutate them. We prove round-trip fidelity including a non-UTF-8 byte.
"""

from __future__ import annotations

import json

from fastapi.testclient import TestClient

from harness.recorder import app as recorder_app
from harness.recorder.app import app, record_payload


def test_record_payload_is_byte_identical(tmp_path, monkeypatch):
    monkeypatch.setenv("CULPRIT_FIXTURES_DIR", str(tmp_path))
    raw = b'{"resource":"issue"}\xff\x00 trailing'  # includes non-UTF-8 bytes
    dest = record_payload("sentry", {"sentry-hook-resource": "issue"}, raw)

    envelope = json.loads(dest.read_text())
    assert envelope["source"] == "sentry"
    assert envelope["resource"] == "issue"
    # latin-1 round-trip recovers the exact wire bytes.
    assert envelope["raw_body"].encode("latin-1") == raw
    # fixtures/sentry/issue/<file>.json
    assert dest.parent.name == "issue"
    assert dest.parent.parent.name == "sentry"


def test_catch_all_endpoint_records(tmp_path, monkeypatch):
    monkeypatch.setenv("CULPRIT_FIXTURES_DIR", str(tmp_path))
    client = TestClient(app)
    body = b'{"hello":"world"}'
    resp = client.post(
        "/sentry",
        content=body,
        headers={"Sentry-Hook-Resource": "event_alert"},
    )
    assert resp.status_code == 200

    files = list((tmp_path / "sentry").rglob("*.json"))
    assert len(files) == 1
    assert files[0].parent.name == "event_alert"
    envelope = json.loads(files[0].read_text())
    assert envelope["raw_body"].encode("latin-1") == body
    assert envelope["headers"]["sentry-hook-resource"] == "event_alert"


def test_health_ok():
    client = TestClient(app)
    assert client.get("/health").json() == {"status": "ok"}
    # module import sanity
    assert recorder_app.app is app
