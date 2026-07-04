"""Service configuration — a single ``pydantic-settings`` layer over env/.env.

Mirrors ``harness/config.py`` (one module of constants + env overrides), but for
the long-running service the values are secrets and connection strings, so they
come from the environment (``.env`` gitignored) via ``pydantic-settings`` rather
than being hardcoded paths.

Every secret is optional: absent secrets make the corresponding integration
inert (and its tests skip), so the deterministic pipeline is fully runnable with
no secrets at all. ``extra="ignore"`` lets the harness's own env vars
(``SENTRY_ORG``, ``NGROK_DOMAIN``, …) share the same ``.env`` without erroring.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

# Culprit repo root = parent of the culprit package directory (mirrors
# harness/config.py). Used to locate alembic.ini and the committed corpus.
REPO_ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(REPO_ROOT / ".env"),
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # Postgres (async, asyncpg). Default matches docker-compose.yml (host :5432).
    database_url: str = "postgresql+asyncpg://culprit:culprit@localhost:5432/culprit"

    # Webhook signing secrets — HMAC over the raw request body.
    sentry_client_secret: str | None = None
    culprit_gh_webhook_secret: str | None = None

    # Read-only evidence access (public fork; token lifts the rate limit).
    github_token: str | None = None
    github_repo: str = "IshanA2007/theCourseForum2"  # the harness fork

    # Outbound integrations.
    discord_webhook_url: str | None = None
    anthropic_api_key: str | None = None

    # Similar-incident embeddings via Voyage (Anthropic's recommended provider).
    # Absent -> similar-incident search is inert and its live tests skip.
    voyage_api_key: str | None = None

    # Correlation window (dedup): the first qualifying signal opens an incident;
    # same-correlation-key signals within this many seconds join it (HANDOFF §4).
    correlation_window_seconds: int = 600


@lru_cache
def get_settings() -> Settings:
    """Process-wide settings singleton (reads env/.env once)."""
    return Settings()
