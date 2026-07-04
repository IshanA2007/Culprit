"""Diagnosis synthesizer — ranked hypotheses with citations, never one answer.

Plan decision 14 / HANDOFF §3: Culprit never asserts a single root cause. It
assembles, deterministically, a lead hypothesis (a code culprit from ranking, or
an infra class from the error type) plus at least one alternative — or an explicit
insufficient-evidence floor. Confidence bands map from deterministic score ratios
with fixed thresholds. The result is persisted to ``incidents.diagnosis`` (the M4
postmortem input); the LLM phrases the narrative only, never the hypotheses.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field

from culprit.impact import Impact
from culprit.ranking import INFRA_ERROR_TYPES, RankingResult

# Fixed confidence thresholds (deterministic; no learning).
_HIGH_SCORE = 6.0
_MED_SCORE = 3.0
_SEPARATION = 3.0

# Error class -> infra hypothesis statement. Known classes get "high" confidence;
# an unrecognized class is still an infra lead, but "medium".
_INFRA_STATEMENTS = {
    "ConnectionError": (
        "Infrastructure outage — a Redis/ElastiCache or database connection "
        "failure is flooding every request; no code culprit is implicated."
    ),
    "RedisError": (
        "Infrastructure outage — the Redis/ElastiCache node is unreachable; "
        "cachalot wraps every ORM read, so near-every page fails."
    ),
    "OperationalError": (
        "Infrastructure outage — the database is unreachable or out of "
        "connections; no code culprit is implicated."
    ),
    "TimeoutError": (
        "Latency/resource fault — requests are timing out, likely dependency or "
        "resource saturation rather than a code change."
    ),
    "MemoryError": (
        "Resource exhaustion — the task is running out of memory (OOM); workers "
        "are being killed before they can report."
    ),
    "BrokenPipeError": (
        "Infrastructure/resource fault — workers are dying mid-request (e.g. OOM "
        "or a killed dependency)."
    ),
}


@dataclass(frozen=True)
class Hypothesis:
    """One ranked hypothesis with a confidence band and cited evidence row ids."""

    kind: str  # code_culprit | infra | alternative | insufficient_evidence
    statement: str
    confidence: str  # high | medium | low
    evidence_ids: list[int] = field(default_factory=list)
    subject: str | None = None  # suspect sha, or the infra error class


@dataclass(frozen=True)
class Diagnosis:
    """Ranked hypotheses + the offered runbook + an impact snapshot (M4 input)."""

    hypotheses: list[Hypothesis]
    runbook_id: str | None = None
    impact: dict | None = None

    def top(self) -> Hypothesis:
        return self.hypotheses[0]

    def as_dict(self) -> dict:
        return {
            "hypotheses": [asdict(h) for h in self.hypotheses],
            "runbook_id": self.runbook_id,
            "impact": self.impact,
        }


def _culprit_confidence(ranked) -> str:
    top = ranked[0].score if ranked else 0.0
    second = ranked[1].score if len(ranked) > 1 else 0.0
    if top >= _HIGH_SCORE and (top - second) >= _SEPARATION:
        return "high"
    if top >= _MED_SCORE:
        return "medium"
    return "low"


def _evidence_ids_for(evidence: list[dict], sha: str) -> list[int]:
    return sorted(e["id"] for e in evidence if e.get("commit_sha") == sha)


def _infra_hypothesis(error_type: str | None, *, kind: str = "infra") -> Hypothesis:
    statement = _INFRA_STATEMENTS.get(
        error_type or "",
        f"Infrastructure fault ({error_type or 'unknown class'}) — no code "
        "culprit is implicated by the stack trace.",
    )
    known = error_type in _INFRA_STATEMENTS
    confidence = (
        "high"
        if (known and kind == "infra")
        else "low"
        if kind != "infra"
        else "medium"
    )
    return Hypothesis(kind, statement, confidence, [], subject=error_type)


def _alternative_floor(error_type: str | None) -> Hypothesis:
    """The mandatory second hypothesis when ranking offers no scored alternative."""
    if error_type in INFRA_ERROR_TYPES:
        return _infra_hypothesis(error_type, kind="alternative")
    return Hypothesis(
        "insufficient_evidence",
        "Insufficient additional evidence for a second suspect; an infrastructural "
        "or out-of-window cause cannot be ruled out.",
        "low",
        [],
        None,
    )


def build_diagnosis(
    result: RankingResult,
    *,
    error_type: str | None,
    evidence: list[dict] | None = None,
    runbook_id: str | None = None,
    impact: Impact | None = None,
) -> Diagnosis:
    """Assemble ranked hypotheses deterministically. Reads no ground-truth labels."""
    evidence = evidence or []
    hypotheses: list[Hypothesis] = []

    if result.verdict == "culprit" and result.ranked:
        top = result.ranked[0]
        hypotheses.append(
            Hypothesis(
                "code_culprit",
                f"Likely code culprit: commit {top.sha[:8]} — {top.reason}",
                _culprit_confidence(result.ranked),
                _evidence_ids_for(evidence, top.sha),
                subject=top.sha,
            )
        )
        for cand in result.ranked[1:3]:
            if cand.score > 0:
                hypotheses.append(
                    Hypothesis(
                        "alternative",
                        f"Alternative suspect: commit {cand.sha[:8]} — {cand.reason}",
                        "low",
                        _evidence_ids_for(evidence, cand.sha),
                        subject=cand.sha,
                    )
                )
        if len(hypotheses) == 1:  # never a single asserted answer
            hypotheses.append(_alternative_floor(error_type))

    elif result.abstain_kind == "infrastructural":
        hypotheses.append(_infra_hypothesis(error_type))
        hypotheses.append(
            Hypothesis(
                "alternative",
                "A commit in the recent deploy window could contribute, but no "
                "window commit is implicated by the stack trace.",
                "low",
                [],
                None,
            )
        )

    else:  # low-confidence abstention — the insufficient-evidence floor leads
        hypotheses.append(
            Hypothesis(
                "insufficient_evidence",
                result.reason
                or "No code culprit — insufficient evidence in the deploy window.",
                "low",
                [],
                None,
            )
        )
        hypotheses.append(_alternative_floor(error_type))

    return Diagnosis(
        hypotheses=hypotheses,
        runbook_id=runbook_id,
        impact=impact.as_dict() if impact else None,
    )
