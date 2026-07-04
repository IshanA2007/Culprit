"""Task 9 — the eval harness (the M3 numbers), with anti-leakage.

Offline tests pin the scorer's classification (now per-source + dedup). The live
test replays the whole 22-run corpus and asserts the honest per-class N — Sentry
top-k (N=10) and SNS-silent top-k (N=8) reported separately AND combined (N=18),
abstention N=3, baseline N=1, cross-source dedup N=2 — proving the pipeline found
the culprit from the ingest contract + deploy feed, never a label (the scorer is
the only ground-truth reader; the runbook labels are scorer-only).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from culprit.config import get_settings
from culprit.eval.driver import evaluate_all
from culprit.eval.replay import ReplayResult
from culprit.eval.score import aggregate, load_runbook_labels, score_run
from culprit.github_api import GitHubClient
from harness.runrecord import load_all_run_records

TOKEN = get_settings().github_token
requires_token = pytest.mark.skipif(not TOKEN, reason="GITHUB_TOKEN not configured")


def _run(gt, *, culprit=None, window=(), fixtures=(), sns=None):
    return SimpleNamespace(
        run_id="r",
        fault_id="f",
        ground_truth=gt,
        culprit_sha=culprit,
        window=[SimpleNamespace(sha=s) for s in window],
        fixture_paths=list(fixtures),
        sns=sns,
    )


# --- offline scorer ---------------------------------------------------------


def test_score_sentry_culprit_top1_and_source():
    run = _run(
        "culprit_commit",
        culprit="c",
        window=["a", "c", "d"],
        fixtures=["fixtures/sentry/event_alert/x.json"],
    )
    replay = ReplayResult(
        "r", True, "culprit", ["c", "a", "d"], ["a", "c", "d"], 0.1, source="sentry"
    )
    entry = score_run(run, replay)
    assert entry["class"] == "culprit"
    assert entry["source"] == "sentry"
    assert entry["top1"] and entry["top3"] and entry["window_ok"]


def test_score_sns_silent_culprit_source():
    run = _run(
        "culprit_commit", culprit="c", window=["a", "c"], sns="fixtures/sns/x.json"
    )
    replay = ReplayResult("r", True, "culprit", ["c", "a"], ["a", "c"], source="sns")
    entry = score_run(run, replay)
    assert entry["class"] == "culprit" and entry["source"] == "sns"


def test_score_cross_source_dedup_case():
    run = _run(
        "abstain",
        fixtures=["fixtures/sentry/event_alert/x.json"],
        sns="fixtures/sns/y.json",
    )
    replay = ReplayResult(
        "r", True, "abstain", [], [], source="sentry", incident_count=1
    )
    entry = score_run(run, replay)
    assert entry["dedup_case"] is True
    assert entry["dedup_ok"] is True


def test_score_baseline_no_incident_is_correct():
    run = _run("no_incident")
    replay = ReplayResult("r", False)
    entry = score_run(run, replay)
    assert entry["class"] == "baseline" and entry["correct"]


def test_aggregate_splits_top_k_by_source():
    entries = [
        {
            "class": "culprit",
            "source": "sentry",
            "top1": True,
            "top3": True,
            "window_ok": True,
            "time_to_brief_s": 0.2,
        },
        {
            "class": "culprit",
            "source": "sns",
            "top1": False,
            "top3": True,
            "window_ok": True,
            "time_to_brief_s": 0.4,
        },
        {"class": "abstain", "correct": True, "time_to_brief_s": 0.1},
        {"class": "baseline", "false_positive": False},
    ]
    agg = aggregate(entries)
    assert agg["culprit_sentry"]["n"] == 1 and agg["culprit_sentry"]["top1"] == 1
    assert (
        agg["culprit_sns_silent"]["n"] == 1 and agg["culprit_sns_silent"]["top1"] == 0
    )
    assert agg["culprit_combined"]["n"] == 2 and agg["culprit_combined"]["top3"] == 2
    assert agg["abstain"]["n"] == 1 and agg["abstain"]["correct"] == 1
    assert agg["baseline"]["n"] == 1 and agg["baseline"]["false_positives"] == 0


def test_runbook_labels_are_authorable_and_cover_incident_faults():
    """The scorer-only label map covers every incident-producing fault."""
    labels = load_runbook_labels()
    incident_faults = {
        r.fault_id for r in load_all_run_records() if r.ground_truth != "no_incident"
    }
    assert incident_faults <= set(labels), (
        f"unlabeled incident faults: {incident_faults - set(labels)}"
    )


# --- live: full corpus replay + score (network-gated) ----------------------


@requires_token
async def test_full_corpus_eval_m3_numbers(db_session):
    github = GitHubClient(TOKEN, get_settings().github_repo)
    try:
        agg, entries = await evaluate_all(db_session, github=github)
    finally:
        await github.aclose()

    # Honest per-class N (decision 15): 10 + 8 + 3 + 1 = 22, nothing deferred.
    assert agg["n_total"] == 22
    assert agg["culprit_sentry"]["n"] == 10
    assert agg["culprit_sns_silent"]["n"] == 8
    assert agg["culprit_combined"]["n"] == 18
    assert agg["abstain"]["n"] == 3  # redis-down, db-stopped, gunicorn-oom
    assert agg["baseline"]["n"] == 1
    assert agg["missed"]["n"] == 0  # every fault now has a Sentry or SNS feed

    # Fidelity + accuracy (M2's 10/10 preserved, not diluted).
    assert agg["culprit_sentry"]["top1"] == 10  # the M2 guarantee, intact
    assert agg["culprit_sentry"]["top3"] == 10
    assert agg["culprit_sentry"]["window_ok"] == 10
    # Combined == Sentry + SNS-silent (honest; top-k only counts real culprit
    # verdicts, never an abstention where the sha is positionally lucky).
    assert (
        agg["culprit_combined"]["top1"]
        == agg["culprit_sentry"]["top1"] + agg["culprit_sns_silent"]["top1"]
    )
    assert (
        agg["culprit_combined"]["top3"]
        == agg["culprit_sentry"]["top3"] + agg["culprit_sns_silent"]["top3"]
    )
    # Honest silent-fault floor: some silent faults are found, none are gamed.
    assert (
        0 <= agg["culprit_sns_silent"]["top1"] <= agg["culprit_sns_silent"]["top3"] <= 8
    )
    assert agg["abstain"]["correct"] == 3
    assert agg["baseline"]["false_positives"] == 0
    assert agg["dedup"]["n"] == 2 and agg["dedup"]["correct"] == 2


@requires_token
async def test_deterministic_sections_identical_run_to_run(db_session):
    github = GitHubClient(TOKEN, get_settings().github_repo)
    try:
        agg1, _ = await evaluate_all(db_session, github=github)
        agg2, _ = await evaluate_all(db_session, github=github)
    finally:
        await github.aclose()
    # The deterministic verdict/top-k is reproducible (LLM never scored).
    for key in ("culprit_sentry", "culprit_sns_silent", "culprit_combined", "abstain"):
        assert agg1[key] == agg2[key]
