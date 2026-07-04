"""Corpus invariants over recorded run records (plan Task 9).

These run over whatever is in ``runs/`` — an empty corpus passes trivially, so
the suite is green before recording and enforces the anti-leakage / labeling
rules once the corpus exists. SHA-resolvability against the fork is gated behind
network + clone availability so CI (which has neither) skips it.
"""

from __future__ import annotations

import subprocess

import pytest

from harness.config import TCF_WORK_DIR
from harness.runrecord import load_all_run_records

RUNS = load_all_run_records()
CODE_RUNS = [r for r in RUNS if r.fault_class == "code"]
ABSTAIN_RUNS = [r for r in RUNS if r.ground_truth == "abstain"]
BASELINE_RUNS = [r for r in RUNS if r.ground_truth == "no_incident"]


def test_code_runs_culprit_in_window_never_release():
    """The core anti-leakage invariant: culprit is a window commit, and for
    multi-commit windows it is never the release (window head)."""
    for r in CODE_RUNS:
        assert r.culprit_sha, f"{r.run_id}: code run missing culprit_sha"
        assert r.culprit_in_window(), f"{r.run_id}: culprit not in recorded window"
        if len(r.window) > 1:
            assert not r.culprit_is_release(), (
                f"{r.run_id}: culprit == release SHA (label leakage)"
            )


def test_multicommit_culprit_off_head_majority():
    """In >=1/3 of multi-commit code cases the culprit is not the head."""
    multi = [r for r in CODE_RUNS if len(r.window) > 1]
    if not multi:
        pytest.skip("no multi-commit code runs recorded yet")
    off_head = sum(1 for r in multi if not r.culprit_is_release())
    assert off_head >= len(multi) / 3


def test_abstain_runs_have_no_culprit_but_carry_release():
    for r in ABSTAIN_RUNS:
        assert r.culprit_sha is None, f"{r.run_id}: abstain run must have no culprit"
        assert r.release_sha, f"{r.run_id}: abstain run must carry a (benign) release"
        assert not any(c.is_culprit for c in r.window)


def test_baseline_runs_have_no_incident():
    for r in BASELINE_RUNS:
        assert r.culprit_sha is None
        assert not any(c.is_culprit for c in r.window)


def test_run_ids_unique():
    ids = [r.run_id for r in RUNS]
    assert len(ids) == len(set(ids))


def _fork_has(sha: str) -> bool:
    out = subprocess.run(
        ["git", "cat-file", "-t", sha],
        cwd=TCF_WORK_DIR,
        capture_output=True,
        text=True,
    )
    return out.returncode == 0 and out.stdout.strip() == "commit"


@pytest.mark.skipif(not TCF_WORK_DIR.exists(), reason="no working clone (CI)")
def test_every_recorded_sha_is_resolvable():
    """Recorded window SHAs must stay fetchable (M2 reads diffs/blame at them)."""
    if not RUNS:
        pytest.skip("no runs recorded yet")
    for r in RUNS:
        for c in r.window:
            assert _fork_has(c.sha), f"{r.run_id}: window SHA {c.sha[:10]} not resolvable"
