"""Task 9 — the eval harness (the resume numbers), with anti-leakage.

Offline tests pin the scorer's classification. The live test replays the whole
corpus through the pipeline and asserts the honest per-class N and that the
labeled culprit lands in top-3 — proving the pipeline found it from the ingest
contract + deploy feed, never a label (the scorer is the only ground-truth reader).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from culprit.config import get_settings
from culprit.eval.driver import evaluate_all
from culprit.eval.replay import ReplayResult
from culprit.eval.score import aggregate, score_run
from culprit.github_api import GitHubClient

TOKEN = get_settings().github_token
requires_token = pytest.mark.skipif(not TOKEN, reason="GITHUB_TOKEN not configured")


def _run(gt, *, culprit=None, window=()):
    return SimpleNamespace(
        run_id="r",
        ground_truth=gt,
        culprit_sha=culprit,
        window=[SimpleNamespace(sha=s) for s in window],
    )


# --- offline scorer ---------------------------------------------------------


def test_score_culprit_top1_and_window():
    run = _run("culprit_commit", culprit="c", window=["a", "c", "d"])
    replay = ReplayResult("r", True, "culprit", ["c", "a", "d"], ["a", "c", "d"], 0.1)
    entry = score_run(run, replay)
    assert entry["class"] == "culprit"
    assert (
        entry["top1"] and entry["top3"] and entry["window_ok"] and entry["verdict_ok"]
    )


def test_score_culprit_off_head_still_top3():
    run = _run("culprit_commit", culprit="c", window=["a", "c", "d", "head"])
    replay = ReplayResult(
        "r", True, "culprit", ["a", "c", "d", "head"], ["a", "c", "d", "head"]
    )
    entry = score_run(run, replay)
    assert entry["top1"] is False
    assert entry["top3"] is True


def test_score_abstain_correct():
    run = _run("abstain")
    replay = ReplayResult("r", True, "abstain", [], [])
    entry = score_run(run, replay)
    assert entry["class"] == "abstain" and entry["correct"]


def test_score_baseline_no_incident_is_correct():
    run = _run("no_incident")
    replay = ReplayResult("r", False)
    entry = score_run(run, replay)
    assert entry["class"] == "baseline" and entry["correct"]


def test_score_silent_fault_is_deferred_not_failure():
    run = _run("culprit_commit", culprit="c")
    replay = ReplayResult("r", False)  # no Sentry event -> no incident
    entry = score_run(run, replay)
    assert entry["class"] == "deferred_m3"
    assert "top3" not in entry  # not scored as a miss


def test_aggregate_rolls_per_class_n():
    entries = [
        {
            "class": "culprit",
            "top1": True,
            "top3": True,
            "window_ok": True,
            "time_to_brief_s": 0.2,
        },
        {
            "class": "culprit",
            "top1": False,
            "top3": True,
            "window_ok": True,
            "time_to_brief_s": 0.4,
        },
        {"class": "abstain", "correct": True, "time_to_brief_s": 0.1},
        {"class": "baseline", "false_positive": False},
        {"class": "deferred_m3"},
    ]
    agg = aggregate(entries)
    assert agg["culprit"]["n"] == 2
    assert agg["culprit"]["top1"] == 1
    assert agg["culprit"]["top3"] == 2
    assert agg["abstain"]["n"] == 1 and agg["abstain"]["correct"] == 1
    assert agg["baseline"]["n"] == 1 and agg["baseline"]["false_positives"] == 0
    assert agg["deferred_m3"]["n"] == 1
    assert agg["time_to_brief_median_s"] is not None


# --- live: full corpus replay + score (network-gated) ----------------------


@requires_token
async def test_full_corpus_eval(db_session):
    github = GitHubClient(TOKEN, get_settings().github_repo)
    try:
        agg, entries = await evaluate_all(db_session, github=github)
    finally:
        await github.aclose()

    # Honest per-class N (decision 10).
    assert agg["culprit"]["n"] == 10  # 5 Sentry code faults x 2 windows
    assert agg["abstain"]["n"] == 2  # redis-down, db-stopped
    assert agg["baseline"]["n"] == 1  # benign deploy
    assert agg["deferred_m3"]["n"] == 9  # 8 silent code + gunicorn-oom

    # Fidelity + accuracy.
    assert agg["culprit"]["window_ok"] == 10  # every reconstructed window matches
    assert agg["culprit"]["top3"] == 10  # culprit always in top-3 (the resume metric)
    assert agg["abstain"]["correct"] == 2  # both infra faults abstain
    assert agg["baseline"]["false_positives"] == 0  # benign deploy -> no false brief
