"""Task 7 — LLM wrapper: phrases the verdict; never decides it.

Deterministic scores are authoritative; the LLM writes the human-facing rationale
(Sonnet 5) and cheap summaries (Haiku 4.5). Live-gated on ANTHROPIC_API_KEY.
"""

from __future__ import annotations

import pytest

from culprit.config import get_settings
from culprit.llm import LLM
from culprit.ranking import Candidate, RankingResult
from culprit.runbooks import load_runbooks

RUNBOOKS = load_runbooks()

# A redis-down incident context — a ConnectionError flood, cache-connectivity
# frames, abstention verdict. The offer-only selector should pick the cache
# runbook even though we abstain on the code culprit.
_REDIS_DOWN_CONTEXT = (
    "Incident: ConnectionError: Error -2 connecting to culprit_redis\n"
    "Verdict: abstain (infrastructural)\n"
    "Reason: No code culprit — looks infrastructural (ConnectionError; no window "
    "commit is implicated by the stack trace).\n"
    "Error type: ConnectionError\n"
    "Stack frames: django_cachalot/monkey_patch.py, redis/connection.py\n"
)

API_KEY = get_settings().anthropic_api_key
requires_key = pytest.mark.skipif(
    not API_KEY, reason="ANTHROPIC_API_KEY not configured"
)


def _culprit_result():
    top = Candidate(
        sha="e0a08029ab",
        score=5.0,
        token_hits=1,
        file_overlap=0,
        stem_overlap=1,
        blame_hits=0,
        comment_only=False,
        files=["tcf_website/templates/.../course_instructor.html"],
        reason="changes course_instructor.html; diff mentions 1 error symbol(s)",
    )
    return RankingResult("culprit", None, [top], "Suspect e0a08029: ...")


def test_llm_disabled_without_key_returns_none():
    llm = LLM(api_key=None)
    assert llm.enabled is False


@requires_key
async def test_rationale_mentions_the_suspect_commit():
    llm = LLM(api_key=API_KEY)
    text = await llm.rationale(
        _culprit_result(),
        error_title="NoReverseMatch: Reverse for 'instructor_detail' not found",
    )
    assert isinstance(text, str) and text.strip()
    assert "e0a08029" in text  # cites the suspect SHA


@requires_key
async def test_abstention_rationale_reads_infrastructural():
    llm = LLM(api_key=API_KEY)
    result = RankingResult(
        "abstain",
        "infrastructural",
        [],
        "No code culprit — looks infrastructural (ConnectionError; ...).",
    )
    text = await llm.rationale(
        result, error_title="ConnectionError: Error -2 connecting"
    )
    assert isinstance(text, str) and text.strip()


@requires_key
async def test_summarize_returns_text():
    llm = LLM(api_key=API_KEY)
    out = await llm.summarize("A long incident timeline with several signals joining.")
    assert isinstance(out, str) and out.strip()


async def test_select_runbook_disabled_without_key_returns_none():
    llm = LLM(api_key=None)
    out = await llm.select_runbook(context=_REDIS_DOWN_CONTEXT, corpus=RUNBOOKS)
    assert out is None


async def test_phrase_diagnosis_disabled_without_key_returns_none():
    from culprit.diagnosis import build_diagnosis

    diag = build_diagnosis(_culprit_result(), error_type="NoReverseMatch")
    llm = LLM(api_key=None)
    assert await llm.phrase_diagnosis(diag) is None


@requires_key
async def test_phrase_diagnosis_returns_a_narrative():
    from culprit.diagnosis import build_diagnosis

    diag = build_diagnosis(_culprit_result(), error_type="NoReverseMatch")
    llm = LLM(api_key=API_KEY)
    text = await llm.phrase_diagnosis(diag)
    assert isinstance(text, str) and text.strip()


@requires_key
async def test_selector_picks_redis_runbook_for_a_redis_down_incident():
    llm = LLM(api_key=API_KEY)
    rid = await llm.select_runbook(context=_REDIS_DOWN_CONTEXT, corpus=RUNBOOKS)
    assert rid == "redis-elasticache-down"
