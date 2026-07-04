"""Task 1 — service scaffold: settings load from env; /health answers.

These are the pure-wiring invariants of the FastAPI service. No Postgres needed
(the async engine is constructed lazily and doesn't connect at import time).
"""

from __future__ import annotations

from fastapi.testclient import TestClient


def test_settings_default_correlation_window(monkeypatch):
    """CORRELATION_WINDOW_SECONDS defaults to 600 (10 min) when unset."""
    monkeypatch.delenv("CORRELATION_WINDOW_SECONDS", raising=False)
    from culprit.config import Settings

    settings = Settings(_env_file=None)
    assert settings.correlation_window_seconds == 600


def test_settings_reads_database_url_from_env(monkeypatch):
    """DATABASE_URL is read case-insensitively from the environment."""
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://u:p@h:5432/culprit")
    from culprit.config import Settings

    settings = Settings(_env_file=None)
    assert settings.database_url == "postgresql+asyncpg://u:p@h:5432/culprit"


def test_settings_ignores_unknown_env_vars(monkeypatch):
    """Extra env vars (Sentry org, ngrok, etc. in .env) must not raise."""
    monkeypatch.setenv("SOME_UNRELATED_HARNESS_VAR", "x")
    from culprit.config import Settings

    Settings(_env_file=None)  # should not raise


def test_health_endpoint_returns_ok():
    from culprit.app import app

    client = TestClient(app)
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}
