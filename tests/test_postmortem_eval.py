"""Task 8 — postmortem eval: dry-run completeness over the incident-producing corpus.

Every incident-producing run (18 code + 3 infra = 21) must yield a *complete*
postmortem draft (timeline · culprit-or-abstention · impact-with-method ·
≥1 hypothesis · fix-commit-or-honest-absence). Code faults capture base_sha as the
fixing commit; infra faults resolve via the SNS ALARM->OK auto-detect and honestly
carry no fixing commit. Deterministic + LLM-free — gated on a GitHub token for the
window reads (cached), like the M3 eval.
"""

from __future__ import annotations

import pytest

from culprit.config import get_settings
from culprit.eval.postmortem_eval import (
    completeness_checks,
    evaluate_postmortem_completeness,
)
from culprit.github_api import GitHubClient

TOKEN = get_settings().github_token
requires_token = pytest.mark.skipif(not TOKEN, reason="GITHUB_TOKEN not configured")

_S = get_settings()
requires_github_app = pytest.mark.skipif(
    not (
        _S.github_app_id and _S.github_app_private_key and _S.github_app_installation_id
    ),
    reason="GitHub App (branch+PR write) not configured",
)


def test_completeness_checks_flags_a_missing_section():
    complete = (
        "## Timeline\n- x\n## Impact\nmethod: y\n## Root cause\n1. _[high]_ Likely "
        "code culprit: abc\nFixing commit `abcd1234` shipped"
    )
    checks = completeness_checks(complete)
    assert all(checks.values())
    # drop the impact method -> incomplete
    assert completeness_checks(complete.replace("method: y", "")) != checks


@requires_token
async def test_every_incident_run_yields_a_complete_postmortem(db_session):
    github = GitHubClient(TOKEN, get_settings().github_repo)
    try:
        result = await evaluate_postmortem_completeness(db_session, github=github)
    finally:
        await github.aclose()

    assert result["n"] == 21  # 18 code + 3 infra; the baseline produced no incident
    assert result["complete"] == 21, [
        r["run_id"] for r in result["rows"] if not r["complete"]
    ]
    # code faults capture base_sha as the fixing commit; infra resolve via sns_ok
    assert result["fix_captured"] == {"n": 18, "correct": 18}
    assert result["sns_ok_resolved"] == {"n": 3, "correct": 3}


@requires_token
async def test_completeness_is_deterministic_run_to_run(db_session):
    github = GitHubClient(TOKEN, get_settings().github_repo)
    try:
        a = await evaluate_postmortem_completeness(db_session, github=github)
        b = await evaluate_postmortem_completeness(db_session, github=github)
    finally:
        await github.aclose()
    assert a["complete"] == b["complete"]
    assert [r["run_id"] for r in a["rows"]] == [r["run_id"] for r in b["rows"]]


def test_assembly_reads_no_ground_truth():
    """Anti-leakage: the postmortem assembly never references a ground-truth label
    (culprit/eval/score.py stays the only ground-truth reader)."""
    import inspect

    import culprit.postmortem as pm

    src = inspect.getsource(pm)
    # dotted attribute access on a run/window object is what leakage would look
    # like (the assembly reads only Incident data; `_culprit_sha` reads the
    # diagnosis hypotheses, not the ground-truth label).
    for banned in (
        ".is_culprit",
        ".ground_truth",
        ".culprit_sha",
        ".culprit_in_window",
    ):
        assert banned not in src, f"postmortem assembly references {banned}"


@requires_token
async def test_llm_narrative_preserves_the_deterministic_facts(db_session):
    """GATED (Anthropic): the LLM Summary replaces only prose — the culprit sha and
    every required section survive (narrative fidelity)."""
    import re

    settings = get_settings()
    if not settings.anthropic_api_key:
        pytest.skip("ANTHROPIC_API_KEY not configured")

    from culprit.eval.postmortem_eval import _replay_resolve_draft
    from culprit.llm import LLM
    from culprit.postmortem import draft_postmortem
    from harness.runrecord import load_all_run_records

    run = next(
        r
        for r in load_all_run_records()
        if r.fault_id == "template-noreversematch-instructor-card"
    )
    github = GitHubClient(TOKEN, settings.github_repo)
    try:
        incident, out = await _replay_resolve_draft(db_session, run, github=github)
        det_body = out[0].body
        culprit8 = re.search(r"culprit: ([0-9a-f]{8})", det_body).group(1)

        narrative = await LLM(settings.anthropic_api_key).phrase_postmortem(
            {"title": incident.correlation_key, "culprit": culprit8}
        )
        row = await draft_postmortem(
            db_session, incident, repo=settings.github_repo, narrative=narrative
        )
    finally:
        await github.aclose()

    assert all(completeness_checks(row.body).values())  # still complete
    assert culprit8 in row.body  # the culprit fact survived the LLM prose
    assert narrative.strip() in row.body  # the narrative is present in Summary


@requires_token
@requires_github_app
async def test_live_pr_opens_to_a_sandbox_and_cleans_up(db_session):
    """GATED (GitHub App): open ONE real postmortem PR to the fork, then clean up
    (close the PR + delete the branch). Proves the live write path end-to-end."""
    from culprit.eval.postmortem_eval import evaluate_live_pr
    from culprit.github_app import GitHubAppWriter

    settings = get_settings()
    github = GitHubClient(TOKEN, settings.github_repo)
    writer = GitHubAppWriter(
        settings.github_app_id,
        settings.github_app_private_key,
        settings.github_app_installation_id,
        settings.postmortems_repo or settings.github_repo,
    )
    result = None
    try:
        result = await evaluate_live_pr(db_session, github=github, writer=writer)
        assert result["opened"] and result["url"] and result["number"]
    finally:
        if result and result.get("number"):  # clean up the sandbox PR + branch
            await writer.close_pull(result["number"])
            await writer.delete_branch(result["branch"])
        await github.aclose()
        await writer.aclose()
