"""Task 3 — deterministic postmortem assembly.

``build_postmortem`` renders the Markdown from persisted incident data only —
``incidents.diagnosis`` (hypotheses + runbook + impact snapshot), the deploy/signal
timeline, and ``fixing_sha``. The skeleton is fixed and the slug/path/branch are
deterministic, so re-drafting is byte-stable. It reads no ground-truth label.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select

from culprit.models import Deploy, Incident, Postmortem, Signal
from culprit.postmortem import build_postmortem, draft_postmortem

_OPENED = datetime(2026, 7, 4, 3, 45, tzinfo=UTC)
_RESOLVED = datetime(2026, 7, 4, 4, 10, tzinfo=UTC)
_REPO = "IshanA2007/theCourseForum2"


def _culprit_diagnosis() -> dict:
    return {
        "hypotheses": [
            {
                "kind": "code_culprit",
                "statement": (
                    "Likely code culprit: commit abcd1234 — touched search/views.py"
                ),
                "confidence": "high",
                "evidence_ids": [1, 2],
                "subject": "abcd1234" + "0" * 32,
            },
            {
                "kind": "alternative",
                "statement": "Alternative suspect: commit ef567890 — search/forms.py",
                "confidence": "low",
                "evidence_ids": [3],
                "subject": "ef567890" + "0" * 32,
            },
        ],
        "runbook_id": "search-zero-results",
        "impact": {
            "failed_requests": {
                "value": 128,
                "method": "Sentry issue.count (server-side error events, near-exact)",
            },
            "affected_users": {
                "value": 37,
                "method": "Sentry userCount (IP-keyed per Sentry docs — an estimate)",
            },
            "window": "12 min",
        },
    }


def _culprit_incident() -> Incident:
    return Incident(
        id=7,
        correlation_key="FieldError: cannot resolve keyword 'x'",
        opened_at=_OPENED,
        resolved_at=_RESOLVED,
        status="resolved",
        severity=2,
        release="r" * 40,
        diagnosis=_culprit_diagnosis(),
        fixing_sha="f" * 40,
        resolution_source="manual",
    )


def _signals() -> list[Signal]:
    return [
        Signal(
            source="sentry",
            kind="event_alert",
            fingerprint="FieldError: cannot resolve keyword 'x'",
            received_at=_OPENED,
            frames=[],
        )
    ]


def _deploys() -> list[Deploy]:
    return [
        Deploy(head_sha="r" * 40, run_started_at=_OPENED, branch="master"),
        Deploy(head_sha="f" * 40, run_started_at=_RESOLVED, branch="master"),
    ]


def _draft(incident=None, signals=None, deploys=None, **kw):
    return build_postmortem(
        incident=incident or _culprit_incident(),
        signals=signals if signals is not None else _signals(),
        deploys=deploys if deploys is not None else _deploys(),
        repo=_REPO,
        **kw,
    )


def test_draft_has_all_required_sections():
    body = _draft().body
    assert "## Timeline" in body
    assert "## Impact" in body and "method:" in body
    assert "## Root cause" in body
    assert "abcd1234" in body  # the culprit short sha
    assert "high" in body.lower()  # the confidence band
    assert "ffffffff" in body  # the fixing commit short sha (8×'f')


def test_offered_runbook_is_present_and_offer_only():
    body = _draft().body
    assert "search-zero-results" in body or "Search returns zero" in body
    assert "never executes" in body.lower() or "offer" in body.lower()


def test_slug_path_branch_are_deterministic_and_byte_stable():
    d1, d2 = _draft(), _draft()
    assert d1.path == d2.path
    assert d1.body == d2.body  # byte-stable render
    assert d1.path.startswith("postmortems/2026-07-04-")
    assert d1.path.endswith(".md")
    assert d1.branch == f"culprit/postmortem-7-{d1.slug}"


def test_abstention_incident_says_no_code_culprit_and_infra_remediation():
    diagnosis = {
        "hypotheses": [
            {
                "kind": "infra",
                "statement": (
                    "Infrastructure outage — a Redis/ElastiCache connection failure "
                    "is flooding every request; no code culprit is implicated."
                ),
                "confidence": "high",
                "evidence_ids": [],
                "subject": "ConnectionError",
            }
        ],
        "runbook_id": "redis-elasticache-down",
        "impact": {
            "failed_requests": {"value": 540, "method": "error lines in the logs"},
            "affected_users": None,
            "window": None,
        },
    }
    inc = Incident(
        id=9,
        correlation_key="ConnectionError: Error 111 connecting to redis",
        opened_at=_OPENED,
        resolved_at=_RESOLVED,
        status="resolved",
        severity=3,
        release=None,
        diagnosis=diagnosis,
        fixing_sha=None,  # infra remediation — no code fix
        resolution_source="sns_ok",
    )
    signals = [
        Signal(
            source="cloudwatch",
            kind="alarm",
            fingerprint="tcf-prod-elasticache-health",
            received_at=_OPENED,
            frames=[],
        )
    ]
    deploys = [
        Deploy(
            head_sha="a" * 40,
            run_started_at=_OPENED - timedelta(minutes=30),
            branch="master",
        )
    ]
    body = build_postmortem(
        incident=inc, signals=signals, deploys=deploys, repo=_REPO
    ).body
    assert "no code culprit" in body.lower() or "infrastructural" in body.lower()
    assert "infrastructure remediation" in body.lower()  # honest no-fix


def test_narrative_overrides_the_summary_prose_only():
    plain = _draft().body
    narrated = _draft(
        narrative="A search deploy broke keyword resolution for 12 min."
    ).body
    # the LLM sentence appears; the deterministic facts are unchanged
    assert "A search deploy broke keyword resolution" in narrated
    assert "abcd1234" in narrated and "## Timeline" in narrated
    assert plain != narrated


# --- persistence orchestrator (idempotent one-row-per-incident) --------------


async def test_draft_postmortem_persists_one_row_idempotently(db_session):
    inc = Incident(
        correlation_key="FieldError: cannot resolve keyword 'x'",
        opened_at=_OPENED,
        resolved_at=_RESOLVED,
        status="resolved",
        severity=2,
        release="r" * 40,
        diagnosis=_culprit_diagnosis(),
        fixing_sha="f" * 40,
        resolution_source="manual",
    )
    db_session.add(inc)
    db_session.add(Deploy(head_sha="r" * 40, run_started_at=_OPENED, branch="master"))
    db_session.add(Deploy(head_sha="f" * 40, run_started_at=_RESOLVED, branch="master"))
    await db_session.flush()
    db_session.add(
        Signal(
            source="sentry",
            kind="event_alert",
            dedup_key="s1",
            fingerprint="FieldError: cannot resolve keyword 'x'",
            received_at=_OPENED,
            frames=[],
            incident_id=inc.id,
        )
    )
    await db_session.commit()

    pm1 = await draft_postmortem(db_session, inc, repo=_REPO)
    assert pm1.state == "drafted"
    assert "## Timeline" in pm1.body
    assert pm1.path.startswith("postmortems/2026-07-04-")

    pm2 = await draft_postmortem(db_session, inc, repo=_REPO)  # re-draft
    assert pm2.id == pm1.id  # idempotent — one row per incident
    count = (
        await db_session.execute(select(func.count()).select_from(Postmortem))
    ).scalar_one()
    assert count == 1
