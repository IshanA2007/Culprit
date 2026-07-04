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
