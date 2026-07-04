"""Task 8 — the pipeline loop: analyse -> brief, living message on re-run.

Offline test drives the post-then-edit logic with a fake Discord. The live test
runs a real recorded incident end-to-end (window -> evidence -> rank -> brief)
against the fork + Anthropic + Discord.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from culprit.config import REPO_ROOT, get_settings
from culprit.correlation import correlate_signal
from culprit.github_api import GitHubClient
from culprit.ingest.github import ingest_github
from culprit.ingest.sentry import ingest_sentry
from culprit.llm import LLM
from culprit.models import Deploy, Incident, Signal
from culprit.pipeline import run_pipeline
from harness.runrecord import load_all_run_records

RUNS = load_all_run_records()
TOKEN = get_settings().github_token
WEBHOOK = get_settings().discord_webhook_url
requires_live = pytest.mark.skipif(
    not (TOKEN and WEBHOOK), reason="GITHUB_TOKEN + DISCORD_WEBHOOK_URL required"
)


class _FakeDiscord:
    enabled = True

    def __init__(self):
        self.posts = []
        self.edits = []

    async def post(self, payload):
        self.posts.append(payload)
        return "msg-1"

    async def edit(self, message_id, payload):
        self.edits.append((message_id, payload))


async def test_second_run_edits_the_living_message(db_session):
    incident = Incident(
        status="open",
        release="2126ec08b659479e2231601ccf2683e5a034a222",
        correlation_key="NoReverseMatch: boom",
        verdict="culprit",  # preset -> analysis is skipped, no GitHub needed
        ranked=[{"sha": "e0a08029ab12", "score": 5.0, "reason": "changes template"}],
    )
    db_session.add(incident)
    await db_session.flush()
    db_session.add(
        Signal(
            source="sentry",
            kind="event_alert",
            dedup_key="k1",
            incident_id=incident.id,
            fingerprint="NoReverseMatch: boom",
            frames=[{"file": "course_instructor.py", "lineno": 178}],
            count=3,
            users=1,
            raw={},
        )
    )
    await db_session.commit()

    fake = _FakeDiscord()
    await run_pipeline(db_session, incident, github=None, discord=fake)
    assert len(fake.posts) == 1 and len(fake.edits) == 0
    assert incident.brief_message_id == "msg-1"

    await run_pipeline(db_session, incident, github=None, discord=fake)
    assert len(fake.posts) == 1 and len(fake.edits) == 1  # edited, not re-posted


class _FakeSelector:
    """Offer-only RunbookSelector stub — returns a preset corpus id."""

    enabled = True

    def __init__(self, runbook_id: str | None):
        self.runbook_id = runbook_id

    async def select_runbook(self, *, context, corpus):
        return self.runbook_id


async def _preset_culprit_incident(db_session):
    incident = Incident(
        status="open",
        release="2126ec08b659479e2231601ccf2683e5a034a222",
        correlation_key="NoReverseMatch: boom",
        verdict="culprit",  # preset -> analysis skipped, no GitHub needed
        ranked=[{"sha": "e0a08029ab12", "score": 5.0, "reason": "changes template"}],
    )
    db_session.add(incident)
    await db_session.flush()
    db_session.add(
        Signal(
            source="sentry",
            kind="event_alert",
            dedup_key="rb1",
            incident_id=incident.id,
            fingerprint="NoReverseMatch: boom",
            frames=[{"file": "course_instructor.py", "lineno": 178}],
            count=3,
            users=1,
            raw={},
        )
    )
    await db_session.commit()
    return incident


async def test_pipeline_offers_runbook_when_selector_enabled(db_session):
    incident = await _preset_culprit_incident(db_session)
    _, payload = await run_pipeline(
        db_session,
        incident,
        github=None,
        discord=_FakeDiscord(),
        runbook_selector=_FakeSelector("rollback-bad-deploy"),
    )
    assert "Suggested runbook" in payload["content"]
    assert "Roll back a bad deploy" in payload["content"]  # resolved from corpus


async def test_pipeline_omits_runbook_without_selector(db_session):
    incident = await _preset_culprit_incident(db_session)
    _, payload = await run_pipeline(
        db_session, incident, github=None, discord=_FakeDiscord()
    )
    assert "Suggested runbook" not in payload["content"]


async def test_pipeline_ignores_selector_id_not_in_corpus(db_session):
    incident = await _preset_culprit_incident(db_session)
    _, payload = await run_pipeline(
        db_session,
        incident,
        github=None,
        discord=_FakeDiscord(),
        runbook_selector=_FakeSelector("hallucinated-runbook"),
    )
    assert "Suggested runbook" not in payload["content"]


async def _setup_incident(session, run):
    """Seed base deploy, ingest the run's deploy + Sentry signals -> incident."""
    session.add(
        Deploy(
            head_sha=run.base_sha,
            branch="master",
            run_started_at=datetime(2020, 1, 1, tzinfo=UTC),
        )
    )
    await session.flush()
    deploy_env = json.loads((REPO_ROOT / run.deploy).read_text())
    await ingest_github(session, deploy_env["raw_body"].encode("latin-1"))

    incident = None
    for rel in run.fixture_paths:
        if "sentry" not in rel:
            continue
        env = json.loads((REPO_ROOT / rel).read_text())
        signal = await ingest_sentry(
            session,
            env["raw_body"].encode("latin-1"),
            datetime.fromisoformat(env["received_at"]),
        )
        incident = await correlate_signal(session, signal, 600)
    return incident


@requires_live
async def test_live_culprit_incident_posts_brief(db_session):
    run = next(r for r in RUNS if r.fault_class == "code" and len(r.window) > 1)
    incident = await _setup_incident(db_session, run)
    github = GitHubClient(TOKEN, get_settings().github_repo)
    llm = LLM(get_settings().anthropic_api_key)
    from culprit.brief import DiscordClient

    discord = DiscordClient(WEBHOOK)
    try:
        result, payload = await run_pipeline(
            db_session, incident, github=github, llm=llm, discord=discord
        )
    finally:
        await github.aclose()
        await discord.aclose()

    assert result.verdict == "culprit"
    assert run.culprit_sha in result.top_shas(3)
    assert incident.brief_message_id  # posted to Discord
    assert "failed request" in payload["content"]


@requires_live
async def test_live_infra_incident_posts_abstention_brief(db_session):
    run = next(r for r in RUNS if r.ground_truth == "abstain" and r.deploy)
    incident = await _setup_incident(db_session, run)
    if incident is None:
        pytest.skip("infra run has no Sentry signal")
    github = GitHubClient(TOKEN, get_settings().github_repo)
    from culprit.brief import DiscordClient

    discord = DiscordClient(WEBHOOK)
    try:
        result, payload = await run_pipeline(
            db_session, incident, github=github, llm=None, discord=discord
        )
    finally:
        await github.aclose()
        await discord.aclose()

    assert result.verdict == "abstain"
    assert "No code culprit — looks infrastructural" in payload["content"]
