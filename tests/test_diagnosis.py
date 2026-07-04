"""Task 4 — the diagnosis synthesizer (plan decision 14).

Deterministic hypothesis assembly: a code-culprit hypothesis (from ranking,
citing its evidence row ids), infra classes (from the error class), and ALWAYS at
least one alternative or an explicit insufficient-evidence floor — never a single
asserted answer (HANDOFF §3). Confidence bands map from deterministic score
ratios with fixed thresholds. The LLM phrases the narrative only.
"""

from __future__ import annotations

import json

from culprit.diagnosis import build_diagnosis
from culprit.impact import compute_impact
from culprit.ranking import Candidate, RankingResult


def _culprit_result(top_score=7.0, second_score=1.0):
    top = Candidate(
        sha="e0a08029ab12",
        score=top_score,
        token_hits=1,
        file_overlap=1,
        stem_overlap=1,
        blame_hits=0,
        comment_only=False,
        files=["course_instructor.py"],
        reason="changes course_instructor.py (in the stack trace)",
    )
    second = Candidate(
        sha="ffff1111ffff",
        score=second_score,
        token_hits=0,
        file_overlap=0,
        stem_overlap=0,
        blame_hits=0,
        comment_only=False,
        files=["unrelated.py"],
        reason="no direct link",
    )
    return RankingResult("culprit", None, [top, second], "Suspect e0a08029: ...")


_EVIDENCE = [
    {"id": 11, "commit_sha": "e0a08029ab12", "kind": "diff"},
    {"id": 12, "commit_sha": "e0a08029ab12", "kind": "blame"},
    {"id": 13, "commit_sha": "ffff1111ffff", "kind": "diff"},
]


def test_culprit_diagnosis_leads_with_code_hypothesis_citing_evidence():
    diag = build_diagnosis(
        _culprit_result(), error_type="NoReverseMatch", evidence=_EVIDENCE
    )
    top = diag.top()
    assert top.kind == "code_culprit"
    assert top.subject == "e0a08029ab12"
    # cites exactly the culprit sha's evidence rows
    assert top.evidence_ids == [11, 12]


def test_diagnosis_always_offers_at_least_one_alternative():
    # never a single asserted answer
    diag = build_diagnosis(
        _culprit_result(), error_type="NoReverseMatch", evidence=_EVIDENCE
    )
    assert len(diag.hypotheses) >= 2


def test_infra_diagnosis_leads_with_infra_hypothesis():
    result = RankingResult(
        "abstain",
        "infrastructural",
        [],
        "No code culprit — looks infrastructural (ConnectionError; ...).",
    )
    diag = build_diagnosis(result, error_type="ConnectionError")
    assert diag.top().kind == "infra"
    assert (
        "cache" in diag.top().statement.lower()
        or "redis" in diag.top().statement.lower()
    )
    assert len(diag.hypotheses) >= 2


def test_low_confidence_abstention_uses_insufficient_evidence_floor():
    result = RankingResult(
        "abstain", "low_confidence", [], "No code culprit — insufficient evidence."
    )
    diag = build_diagnosis(result, error_type=None)
    assert diag.top().kind == "insufficient_evidence"
    assert diag.top().confidence == "low"
    assert len(diag.hypotheses) >= 2


def test_confidence_bands_from_score_ratios():
    # clear winner (high score, well-separated) -> high
    strong = build_diagnosis(_culprit_result(7.0, 1.0), error_type="X")
    assert strong.top().confidence == "high"
    # weak winner (low score) -> low
    weak = build_diagnosis(_culprit_result(2.0, 2.0), error_type="X")
    assert weak.top().confidence == "low"


def test_diagnosis_is_deterministic():
    a = build_diagnosis(_culprit_result(), error_type="X", evidence=_EVIDENCE)
    b = build_diagnosis(_culprit_result(), error_type="X", evidence=_EVIDENCE)
    assert a.as_dict() == b.as_dict()


def test_as_dict_is_json_serializable_for_persistence():
    diag = build_diagnosis(
        _culprit_result(),
        error_type="NoReverseMatch",
        evidence=_EVIDENCE,
        runbook_id="app-error-spike-after-deploy",
        impact=compute_impact(sentry_count=17, sentry_users=3),
    )
    d = diag.as_dict()
    # the M4 postmortem input: hypotheses + selected runbook + impact snapshot
    assert d["runbook_id"] == "app-error-spike-after-deploy"
    assert d["impact"]["failed_requests"]["value"] == 17
    assert d["hypotheses"][0]["kind"] == "code_culprit"
    json.dumps(d)  # must not raise


def test_no_hypothesis_is_an_unqualified_single_answer():
    # every hypothesis carries a confidence band; there is always more than one
    for result, et in [
        (_culprit_result(), "NoReverseMatch"),
        (RankingResult("abstain", "infrastructural", [], "r"), "ConnectionError"),
        (RankingResult("abstain", "low_confidence", [], "r"), None),
    ]:
        diag = build_diagnosis(result, error_type=et, evidence=_EVIDENCE)
        assert len(diag.hypotheses) >= 2
        for h in diag.hypotheses:
            assert h.confidence in ("high", "medium", "low")
