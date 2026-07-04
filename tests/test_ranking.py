"""Task 7 — culprit ranking + abstention (deterministic; LLM only phrases).

The scorer combines the Sentry stack-trace signal with the candidate diffs:
frame-file overlap, file-stem affinity (course_instructor.html ~ .py), and the
error's named symbols appearing in a commit's diff. Comment/docstring-only diffs
score zero (a decoy can't crash). Ties preserve compare order (oldest first) so
the release head is never chosen for being newest (anti-leakage). Abstains when
an infra-class error implicates no window commit (HANDOFF §3, decision 7).

The live test replays every Sentry code fault through the real fork and asserts
the labeled culprit lands in top-3 — the resume metric. It never feeds a label
into the ranker (anti-leakage); labels are read only in the assertion.
"""

from __future__ import annotations

import json

import pytest

from culprit.config import REPO_ROOT, get_settings
from culprit.deploys import reconstruct_window
from culprit.github_api import GitHubClient, blame_commit_for_line
from culprit.ingest.sentry import parse_sentry
from culprit.ranking import (
    error_type_from_title,
    extract_error_tokens,
    rank,
    rank_frameless,
)
from harness.runrecord import load_all_run_records

RUNS = load_all_run_records()
TOKEN = get_settings().github_token
requires_token = pytest.mark.skipif(not TOKEN, reason="GITHUB_TOKEN not configured")


# --- pure scorer (offline synthetic) ---------------------------------------


def _cand(sha, files, patch):
    return {"sha": sha, "files": files, "patch": patch}


def test_frame_file_overlap_beats_unrelated_decoy():
    candidates = [
        _cand("decoy", ["browse.py"], "+def browse():\n+    return 1"),
        _cand("culprit", ["search.py"], "+    qs.filter(similarity_score=1)"),
    ]
    result = rank(
        candidates,
        frame_files={"search.py"},
        tokens={"similarity_score"},
        blame_counts={},
        error_type="FieldError",
    )
    assert result.verdict == "culprit"
    assert result.ranked[0].sha == "culprit"


def test_comment_only_diff_scores_zero():
    candidates = [_cand("c", ["search.py"], '+# just a comment\n+"""doc"""')]
    result = rank(
        candidates,
        frame_files={"search.py"},
        tokens=set(),
        blame_counts={},
        error_type="FieldError",
    )
    # comment-only -> zero score -> abstain (no real signal)
    assert result.ranked[0].score == 0


def test_ties_prefer_oldest_not_release_head():
    # both score 0; input order is oldest -> release head. Tie must not surface head.
    candidates = [
        _cand("older", ["a.py"], "+x = 1"),
        _cand("release_head", ["b.py"], "+y = 2"),
    ]
    result = rank(
        candidates,
        frame_files={"unrelated.py"},
        tokens=set(),
        blame_counts={},
        error_type="ValueError",
    )
    assert result.ranked[0].sha == "older"


def test_infra_error_with_no_overlap_abstains():
    candidates = [_cand("d1", ["utils.py"], "+x=1"), _cand("d2", ["browse.py"], "+y=2")]
    result = rank(
        candidates,
        frame_files={"course_instructor.py"},  # no candidate touches it
        tokens=set(),
        blame_counts={},
        error_type="ConnectionError",
    )
    assert result.verdict == "abstain"
    assert result.abstain_kind == "infrastructural"
    assert "infrastructural" in result.reason.lower()


def test_blame_hit_boosts_candidate():
    candidates = [_cand("a", ["x.py"], "+a=1"), _cand("b", ["y.py"], "+b=2")]
    result = rank(
        candidates,
        frame_files=set(),
        tokens=set(),
        blame_counts={"b": 2},
        error_type="TypeError",
    )
    assert result.verdict == "culprit"
    assert result.ranked[0].sha == "b"


# --- frameless ranking (silent faults, alarm-class affinity — decision 9) ----


def test_frameless_latency_alarm_picks_the_annotation_commit():
    candidates = [
        _cand("decoy", ["browse.py"], "+# tidy up"),
        _cand("culprit", ["stats.py"], "+    qs = qs.annotate(gpa=Avg('gpa'))"),
    ]
    result = rank_frameless(candidates, alarm_metric="TargetResponseTime")
    assert result.verdict == "culprit"
    assert result.ranked[0].sha == "culprit"


def test_frameless_latency_alarm_picks_index_dropping_migration():
    candidates = [
        _cand("decoy", ["views/browse.py"], "+x = 1"),
        _cand(
            "culprit",
            ["tcf_website/migrations/0042_drop.py"],
            "+        migrations.RemoveIndex(model_name='course', name='trgm_idx'),",
        ),
    ]
    result = rank_frameless(candidates, alarm_metric="TargetResponseTime")
    assert result.verdict == "culprit"
    assert result.ranked[0].sha == "culprit"


def test_frameless_search_canary_picks_search_module_commit():
    candidates = [
        _cand("decoy", ["browse.py"], "+x = 1"),
        _cand("culprit", ["tcf_website/search.py"], "+    threshold = 0.9"),
    ]
    result = rank_frameless(candidates, alarm_metric="SuccessPercent")
    assert result.verdict == "culprit"
    assert result.ranked[0].sha == "culprit"


def test_frameless_memory_alarm_abstains_infrastructural():
    candidates = [_cand("a", ["views.py"], "+x = 1")]
    result = rank_frameless(candidates, alarm_metric="MemoryUtilization")
    assert result.verdict == "abstain"
    assert result.abstain_kind == "infrastructural"


def test_frameless_no_affinity_abstains_low_confidence():
    # a latency alarm but no candidate touches a latency surface -> higher bar abstain
    candidates = [_cand("a", ["templates/x.html"], "+<div>hi</div>")]
    result = rank_frameless(candidates, alarm_metric="TargetResponseTime")
    assert result.verdict == "abstain"
    assert result.abstain_kind == "low_confidence"


def test_error_type_and_token_helpers():
    assert (
        error_type_from_title("FieldError: Cannot resolve keyword 'x'") == "FieldError"
    )
    toks = extract_error_tokens(
        "NoReverseMatch: Reverse for 'instructor_detail' not found"
    )
    assert "instructor_detail" in toks


# --- live: ranking over the real corpus (network-gated) --------------------


def _event_alert_rel(run):
    return next((fp for fp in run.fixture_paths if "event_alert" in fp), None)


async def _rank_run(github, run):
    """Assemble ranker inputs from a run's Sentry fixture + the fork. No labels."""
    body = json.loads(
        json.loads((REPO_ROOT / _event_alert_rel(run)).read_text())["raw_body"].encode(
            "latin-1"
        )
    )
    parsed = parse_sentry(body)
    event = body["data"]["event"]
    meta = event.get("metadata") or {}
    tokens = extract_error_tokens(
        event.get("title"), meta.get("value"), meta.get("type")
    )
    error_type = error_type_from_title(event.get("title") or "")
    frame_files = {f["file"] for f in parsed.frames}

    commits = await reconstruct_window(github, run.base_sha, run.release_sha)
    candidates = []
    blame_counts: dict[str, int] = {}
    window_set = {c["sha"] for c in commits}
    for c in commits:
        full = await github.commit(c["sha"])
        candidates.append(
            {
                "sha": c["sha"],
                "files": [f.get("filename") for f in full.get("files", [])],
                "patch": "\n".join(f.get("patch", "") for f in full.get("files", [])),
            }
        )
    for frame in parsed.frames:
        oid = blame_commit_for_line(
            await github.blame(frame["file"], run.release_sha), frame["lineno"]
        )
        if oid in window_set:
            blame_counts[oid] = blame_counts.get(oid, 0) + 1

    return rank(
        candidates,
        frame_files=frame_files,
        tokens=tokens,
        blame_counts=blame_counts,
        error_type=error_type,
    )


@requires_token
async def test_every_code_fault_culprit_in_top3():
    code_runs = [
        r for r in RUNS if r.fault_class == "code" and _event_alert_rel(r) is not None
    ]
    assert len(code_runs) == 10  # 5 Sentry-visible code faults x 2 windows
    github = GitHubClient(TOKEN, get_settings().github_repo)
    try:
        for run in code_runs:
            result = await _rank_run(github, run)
            assert result.verdict == "culprit", run.run_id
            top3 = result.top_shas(3)
            assert run.culprit_sha in top3, f"{run.run_id}: culprit not in top-3 {top3}"
            # never surface the release head merely for being newest
            if len(run.window) > 1:
                assert result.ranked[0].sha != run.release_sha, run.run_id
    finally:
        await github.aclose()


@requires_token
async def test_infra_faults_abstain_infrastructural():
    infra_runs = [
        r
        for r in RUNS
        if r.ground_truth == "abstain" and _event_alert_rel(r) is not None
    ]
    assert infra_runs  # redis-down, db-stopped
    github = GitHubClient(TOKEN, get_settings().github_repo)
    try:
        for run in infra_runs:
            result = await _rank_run(github, run)
            assert result.verdict == "abstain", run.run_id
            assert result.abstain_kind == "infrastructural", run.run_id
    finally:
        await github.aclose()
