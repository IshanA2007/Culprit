"""Task 6 — window reconstruction + evidence pinned to the deployed SHA.

The window is the candidate set an eval ranks over. Production has no ground-truth
window; M2 derives it as compare(previous_head, release).commits (decision 5). The
network-gated tests assert the reconstructed set == the recorded window for real
runs (the key eval-fidelity guarantee); they skip in CI (no GITHUB_TOKEN).
"""

from __future__ import annotations

import pytest

from culprit.config import get_settings
from culprit.deploys import reconstruct_window
from culprit.evidence import gather_evidence
from culprit.github_api import GitHubClient, blame_commit_for_line, window_commit_shas
from culprit.models import Evidence, Incident
from harness.runrecord import load_all_run_records

RUNS = load_all_run_records()
TOKEN = get_settings().github_token
requires_token = pytest.mark.skipif(not TOKEN, reason="GITHUB_TOKEN not configured")


# --- pure helpers (offline) -------------------------------------------------


def test_window_commit_shas_extracts_in_order():
    compare_json = {"commits": [{"sha": "a"}, {"sha": "b"}, {"sha": "c"}]}
    assert window_commit_shas(compare_json) == ["a", "b", "c"]


def test_blame_commit_for_line_matches_covering_range():
    ranges = [
        {"startingLine": 1, "endingLine": 10, "oid": "old"},
        {"startingLine": 11, "endingLine": 20, "oid": "culprit"},
    ]
    assert blame_commit_for_line(ranges, 15) == "culprit"
    assert blame_commit_for_line(ranges, 5) == "old"
    assert blame_commit_for_line(ranges, 99) is None


# --- evidence assembly (offline, fake client) -------------------------------


class _FakeGitHub:
    def __init__(self, commits, blames):
        self._commits = commits
        self._blames = blames

    async def commit(self, sha):
        return self._commits[sha]

    async def blame(self, path, sha):
        return self._blames.get((path, sha), [])


async def test_gather_evidence_builds_diffs_and_blame(db_session):
    incident = Incident(status="open", release="HEAD")
    db_session.add(incident)
    await db_session.flush()

    fake = _FakeGitHub(
        commits={
            "c1": {"files": [{"filename": "a.py"}], "message": "fix a"},
            "c2": {"files": [{"filename": "b.py"}], "message": "fix b"},
        },
        blames={("a.py", "HEAD"): [{"startingLine": 1, "endingLine": 20, "oid": "c1"}]},
    )
    evidence = await gather_evidence(
        db_session,
        fake,
        incident_id=incident.id,
        window_shas=["c1", "c2"],
        frames=[{"file": "a.py", "lineno": 10, "function": "f"}],
        release_sha="HEAD",
    )
    diffs = [e for e in evidence if e.kind == "diff"]
    blames = [e for e in evidence if e.kind == "blame"]
    assert len(diffs) == 2
    assert len(blames) == 1
    assert blames[0].commit_sha == "c1"  # frame at a.py:10 blames to c1
    persisted = (await db_session.execute(Evidence.__table__.select())).all()
    assert len(persisted) == 3


# --- live reconstruction (network-gated) ------------------------------------


def _multicommit_code_run():
    return next(r for r in RUNS if r.fault_class == "code" and len(r.window) > 1)


@requires_token
async def test_reconstructed_window_equals_recorded_window():
    """compare(base_sha, release_sha).commits == the recorded window (decision 5)."""
    run = _multicommit_code_run()
    github = GitHubClient(TOKEN, get_settings().github_repo)
    try:
        commits = await reconstruct_window(github, run.base_sha, run.release_sha)
    finally:
        await github.aclose()
    reconstructed = {c["sha"] for c in commits}
    recorded = {c.sha for c in run.window}
    assert reconstructed == recorded


@requires_token
async def test_every_run_reconstructs_its_recorded_window():
    """Across the whole corpus: compare(base, release) == the recorded window."""
    assert RUNS
    github = GitHubClient(TOKEN, get_settings().github_repo)
    try:
        for run in RUNS:
            commits = await reconstruct_window(github, run.base_sha, run.release_sha)
            reconstructed = {c["sha"] for c in commits}
            recorded = {c.sha for c in run.window}
            assert reconstructed == recorded, run.run_id
    finally:
        await github.aclose()


@requires_token
async def test_all_recorded_shas_resolvable_on_fork():
    """Every window SHA is fetchable at the pinned commit (M2 reads diffs there)."""
    run = _multicommit_code_run()
    github = GitHubClient(TOKEN, get_settings().github_repo)
    try:
        for c in run.window:
            commit = await github.commit(c.sha)
            assert commit["sha"] == c.sha
            assert "files" in commit
    finally:
        await github.aclose()
