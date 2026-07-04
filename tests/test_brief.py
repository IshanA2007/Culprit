"""Task 8 — the Discord brief: render + living-message posting.

Render tests are offline. The Discord post/edit test is gated on
DISCORD_WEBHOOK_URL (present locally; CI has none, so it skips).
"""

from __future__ import annotations

import pytest

from culprit.brief import BriefContext, DiscordClient, render_brief
from culprit.config import get_settings

WEBHOOK = get_settings().discord_webhook_url
requires_webhook = pytest.mark.skipif(
    not WEBHOOK, reason="DISCORD_WEBHOOK_URL not configured"
)


def test_culprit_brief_cites_commit_and_frames():
    ctx = BriefContext(
        title="NoReverseMatch: Reverse for 'instructor_detail' not found",
        verdict="culprit",
        abstain_kind=None,
        reason="Suspect e0a08029: changes course_instructor.html.",
        ranked=[{"sha": "e0a08029ab12", "score": 5.0, "reason": "changes template"}],
        release="2126ec08b659",
        count=17,
        users=3,
        frames=[
            {"file": "tcf_website/views/courses/course_instructor.py", "lineno": 178}
        ],
        repo="IshanA2007/theCourseForum2",
    )
    content = render_brief(ctx)["content"]
    assert "e0a08029" in content  # cites the culprit commit
    assert "course_instructor.py:178" in content  # cites the frame
    assert "failed request" in content  # impact line
    assert "commit/e0a08029ab12" in content  # link to the commit


def test_abstention_brief_reads_infrastructural():
    ctx = BriefContext(
        title="ConnectionError: Error -2 connecting to culprit_redis",
        verdict="abstain",
        abstain_kind="infrastructural",
        reason="No code culprit — looks infrastructural (ConnectionError; ...).",
        release="dd3d21804fca",
        count=5,
        users=2,
    )
    content = render_brief(ctx)["content"]
    assert "No code culprit — looks infrastructural" in content
    assert "failed request" in content


def test_brief_renders_ranked_hypotheses_never_a_single_answer():
    from culprit.diagnosis import build_diagnosis
    from culprit.ranking import Candidate, RankingResult

    top = Candidate(
        sha="e0a08029ab12",
        score=7.0,
        token_hits=1,
        file_overlap=1,
        stem_overlap=1,
        blame_hits=0,
        comment_only=False,
        files=["course_instructor.py"],
        reason="changes course_instructor.py (in the stack trace)",
    )
    result = RankingResult("culprit", None, [top], "Suspect e0a08029: ...")
    diag = build_diagnosis(
        result,
        error_type="NoReverseMatch",
        evidence=[{"id": 11, "commit_sha": "e0a08029ab12", "kind": "diff"}],
    )
    ctx = BriefContext(
        title="x",
        verdict="culprit",
        abstain_kind=None,
        reason="r",
        ranked=[{"sha": "e0a08029ab12", "score": 7.0, "reason": "changes template"}],
        diagnosis=diag,
    )
    content = render_brief(ctx)["content"]
    assert "Diagnosis" in content
    assert "confidence" in content.lower()
    assert "#11" in content  # cites the evidence row id
    # ranked hypotheses -> more than one line under Diagnosis (never a single answer)
    assert "1." in content and "2." in content


def test_brief_cites_similar_past_incidents():
    ctx = BriefContext(
        title="x",
        verdict="culprit",
        abstain_kind=None,
        reason="r",
        ranked=[{"sha": "abc123", "score": 5.0, "reason": "changes template"}],
        similar=[{"id": 7, "title": "NoReverseMatch: boom", "distance": 0.02}],
    )
    content = render_brief(ctx)["content"]
    assert "Similar past incidents" in content
    assert "#7" in content


def test_brief_omits_similar_section_when_none():
    ctx = BriefContext(
        title="x", verdict="abstain", abstain_kind="low_confidence", reason="r"
    )
    assert "Similar past incidents" not in render_brief(ctx)["content"]


def test_brief_impact_states_methodology():
    from culprit.impact import compute_impact

    ctx = BriefContext(
        title="x",
        verdict="culprit",
        abstain_kind=None,
        reason="r",
        ranked=[{"sha": "abc123", "score": 5.0, "reason": "changes template"}],
        impact=compute_impact(sentry_count=17, sentry_users=3),
    )
    content = render_brief(ctx)["content"]
    assert "~17 failed request" in content
    assert "≈3 unique user" in content
    assert "method:" in content  # every number carries its methodology


def test_brief_offers_runbook_offer_only():
    ctx = BriefContext(
        title="ConnectionError: Error -2 connecting to culprit_redis",
        verdict="abstain",
        abstain_kind="infrastructural",
        reason="No code culprit — looks infrastructural.",
        runbook_id="redis-elasticache-down",
        runbook_title="Redis / ElastiCache down",
        runbook_summary="The Redis ElastiCache node is unreachable; cachalot 500s.",
    )
    content = render_brief(ctx)["content"]
    assert "Suggested runbook" in content
    assert "Redis / ElastiCache down" in content
    # The permanent offer-only stance must be visible in the brief itself.
    assert "offer-only" in content.lower() or "never execute" in content.lower()


def test_brief_omits_runbook_section_when_absent():
    ctx = BriefContext(
        title="x", verdict="abstain", abstain_kind="low_confidence", reason="r"
    )
    content = render_brief(ctx)["content"]
    assert "Suggested runbook" not in content


def test_impact_line_hedges_user_estimate():
    ctx = BriefContext(
        title="x",
        verdict="abstain",
        abstain_kind="low_confidence",
        reason="r",
        count=42,
        users=9,
    )
    content = render_brief(ctx)["content"]
    assert "~42 failed request" in content
    assert "≈9 unique user" in content
    assert "estimate" in content.lower()


@requires_webhook
async def test_discord_post_then_edit_is_a_living_message():
    discord = DiscordClient(WEBHOOK)
    try:
        ctx = BriefContext(
            title="Culprit self-test (safe to ignore)",
            verdict="abstain",
            abstain_kind="low_confidence",
            reason="Culprit M2 Task 8 living-message test.",
            count=1,
        )
        message_id = await discord.post(render_brief(ctx))
        assert message_id
        ctx.reason = "Culprit M2 Task 8 living-message test — edited in place."
        await discord.edit(message_id, render_brief(ctx))  # edits, does not re-post
    finally:
        await discord.aclose()
