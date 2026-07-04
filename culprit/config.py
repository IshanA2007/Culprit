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

    # GitHub App — the ONE write permission (M4): branch + file + PR on the fork.
    # All optional/inert: absent -> the writer is inert and drafting stays dry-run.
    github_app_id: str | None = None
    github_app_private_key: str | None = None  # PEM contents
    github_app_private_key_path: str | None = None  # ...or a path to the PEM
    github_app_installation_id: str | None = None
    # Where postmortems are opened (default = the fork) and the directory + branch.
    postmortems_repo: str | None = None  # None -> github_repo
    postmortems_dir: str = "postmortems"
    postmortems_base_branch: str = "master"  # the fork's default branch
    # When true (default), drafting renders the Markdown + PR request WITHOUT
    # pushing — the eval/dry-run path. Off + App configured -> open a real PR.
    postmortem_dry_run: bool = True

    # Outbound integrations.
    discord_webhook_url: str | None = None
    anthropic_api_key: str | None = None

    # Discord interactions + thread read (M4). All optional/inert by default.
    # Ed25519 app public key — verifies /discord/interactions (the /resolve command).
    discord_public_key: str | None = None
    # Read-scoped bot token — reads the incident channel thread for the postmortem.
    discord_bot_token: str | None = None
    # The channel the brief is posted in (thread root for the read path).
    discord_incident_channel_id: str | None = None

    # Similar-incident embeddings via Voyage (Anthropic's recommended provider).
    # Absent -> similar-incident search is inert and its live tests skip.
    voyage_api_key: str | None = None

    # Correlation window (dedup): the first qualifying signal opens an incident;
    # same-correlation-key signals within this many seconds join it (HANDOFF §4).
    correlation_window_seconds: int = 600

    # SNS/CloudWatch ingest (M3). All optional/inert by default.
    aws_region: str = "us-east-1"
    # Comma-separated TopicArn allowlist; empty = accept any topic.
    sns_allowed_topic_arns: str | None = None
    # Require a valid SNS signature (X.509). Off lets a dev POST unsigned fixtures.
    sns_signature_strict: bool = True
    # Dev/fixture cert override: verify against this local PEM instead of fetching
    # SigningCertURL. Unset in production -> fetch from the allowlisted amazonaws.com
    # host. (The synthesized fixtures are signed by the vendored keypair.)
    sns_signing_cert_path: str | None = None

    # When true, ingest routes run the analysis pipeline in the background and post
    # the brief. Off by default so tests never fire live network/Discord calls; the
    # serve smoke-check turns it on.
    autorun_pipeline: bool = False


@lru_cache
def get_settings() -> Settings:
    """Process-wide settings singleton (reads env/.env once)."""
    return Settings()
