"""Deterministic culprit ranking + abstention (plan decision 6/7).

The deterministic score decides the verdict; the LLM (``culprit/llm.py``) only
re-phrases and breaks ties — never overrides a clear winner (HANDOFF §3). Scoring
combines Sentry's suspect-commit signals against the candidate diffs:

* ``file_overlap``  — a stack-frame file is changed by the commit (weight 3)
* ``stem_overlap``  — a changed file shares a basename stem with a frame file,
                      e.g. ``course_instructor.html`` ~ ``course_instructor.py`` (weight 2)
* ``blame_hits``    — a frame line blames to this commit at the release SHA (weight 3)
* ``token_hits``    — an error-named symbol (``instructor_detail``, ``season``)
                      appears in the commit's diff (weight 1)

Comment/docstring-only diffs score 0 — a benign decoy can't cause a runtime error.
Ties preserve compare order (oldest first), so the release head is never surfaced
merely for being newest. Abstains when an infra-class error implicates no window
commit ("No code culprit — looks infrastructural").
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# Connectivity/runtime-environment errors: infrastructural unless a code file in
# the stack trace is directly implicated by the window.
INFRA_ERROR_TYPES = {
    "ConnectionError",
    "OperationalError",
    "TimeoutError",
    "RedisError",
    "MemoryError",
    "BrokenPipeError",
}

# Generic error-grammar words that carry no culprit signal.
_STOP = {
    "error",
    "reverse",
    "valid",
    "view",
    "function",
    "pattern",
    "name",
    "found",
    "which",
    "object",
    "does",
    "exist",
    "column",
    "cannot",
    "resolve",
    "keyword",
    "into",
    "field",
    "choices",
    "duplicate",
    "value",
    "violates",
    "unique",
    "constraint",
    "null",
    "none",
    "line",
    "detail",
    "already",
    "exists",
    "failed",
    "host",
    "errno",
    "connecting",
    "defined",
}

_FILE_W = 3
_STEM_W = 2
_BLAME_W = 3
_TOKEN_W = 1

ABSTAIN_INFRA_REASON = "No code culprit — looks infrastructural"
ABSTAIN_LOWCONF_REASON = "No code culprit — insufficient evidence in the deploy window"


def error_type_from_title(title: str) -> str | None:
    """The exception class from a Sentry title ('FieldError: ...' -> 'FieldError')."""
    if not title:
        return None
    return title.split(":", 1)[0].strip() or None


def extract_error_tokens(*texts: str | None) -> set[str]:
    """Named symbols from the error text (quoted, dotted, and identifier words)."""
    text = " ".join(t for t in texts if t)
    tokens: set[str] = set()
    tokens |= set(re.findall(r"'([^']+)'", text))
    tokens |= set(re.findall(r"[A-Za-z_][A-Za-z0-9_]*\.[A-Za-z0-9_.]+", text))
    tokens |= set(re.findall(r"[A-Za-z_][A-Za-z0-9_]{3,}", text))
    return {t for t in tokens if len(t) >= 4 and t.lower() not in _STOP}


def _stems(files) -> set[str]:
    return {f.rsplit("/", 1)[-1].rsplit(".", 1)[0] for f in files if f}


def _changed_code_lines(patch: str) -> list[str]:
    lines = []
    for line in patch.splitlines():
        if line[:1] in "+-" and not line.startswith(("+++", "---")):
            lines.append(line[1:])
    return lines


def _is_comment_only(patch: str) -> bool:
    """True if every changed line is a comment / docstring / blank (a benign decoy)."""
    code = [ln.strip() for ln in _changed_code_lines(patch) if ln.strip()]
    if not code:
        return True
    return all(ln.startswith(("#", '"""', "'''", '"', "'")) for ln in code)


@dataclass
class Candidate:
    sha: str
    score: float
    token_hits: int
    file_overlap: int
    stem_overlap: int
    blame_hits: int
    comment_only: bool
    files: list[str] = field(default_factory=list)
    reason: str = ""


@dataclass
class RankingResult:
    verdict: str  # "culprit" | "abstain"
    abstain_kind: str | None  # "infrastructural" | "low_confidence" | None
    ranked: list[Candidate]
    reason: str

    def top_shas(self, k: int) -> list[str]:
        return [c.sha for c in self.ranked[:k]]

    def as_dicts(self) -> list[dict]:
        return [
            {"sha": c.sha, "score": c.score, "reason": c.reason} for c in self.ranked
        ]


def _score_candidate(candidate, frame_files, tokens, blame_counts) -> Candidate:
    sha = candidate["sha"]
    files = candidate.get("files") or []
    patch = candidate.get("patch") or ""

    comment_only = _is_comment_only(patch)
    file_overlap = len(frame_files & set(files))
    stem_overlap = len(_stems(files) & _stems(frame_files))
    patch_lower = patch.lower()
    token_hits = sum(1 for t in tokens if t.lower() in patch_lower)
    blame_hits = blame_counts.get(sha, 0)

    if comment_only:
        score = 0.0
    else:
        score = (
            _TOKEN_W * token_hits
            + _FILE_W * file_overlap
            + _STEM_W * stem_overlap
            + _BLAME_W * blame_hits
        )

    return Candidate(
        sha=sha,
        score=score,
        token_hits=token_hits,
        file_overlap=file_overlap,
        stem_overlap=stem_overlap,
        blame_hits=blame_hits,
        comment_only=comment_only,
        files=files,
        reason=_candidate_reason(files, frame_files, token_hits, blame_hits),
    )


def _candidate_reason(files, frame_files, token_hits, blame_hits) -> str:
    bits = []
    touched = sorted(set(files) & frame_files)
    if touched:
        bits.append(f"changes {', '.join(touched)} (in the stack trace)")
    if blame_hits:
        bits.append(f"blamed by {blame_hits} stack frame(s)")
    if token_hits:
        bits.append(f"diff mentions {token_hits} error symbol(s)")
    return "; ".join(bits) or "no direct link to the stack trace"


# --- frameless ranking: alarm-class diff-surface affinity (plan decision 9) --
# When there are no frames anywhere (a silent fault caught only by a CloudWatch
# alarm), score window commits by whether their diff touches the surface the alarm
# class implicates. A deliberately HIGHER abstention bar than the frame path — the
# evidence is thin (a latency alarm names no file), so the honest number governs.

_FRAMELESS_MIN = 3.0
LATENCY_METRICS = {"TargetResponseTime"}
SEARCH_METRICS = {"SuccessPercent"}  # the synthetic search canary
INFRA_METRICS = {
    "HTTPCode_ELB_5XX_Count",
    "HTTPCode_Target_5XX_Count",
    "MemoryUtilization",
    "CurrConnections",
    "DatabaseConnections",
    "CPUUtilization",
}


def _signed_lines(patch: str) -> tuple[list[str], list[str]]:
    added, removed = [], []
    for line in patch.splitlines():
        if line.startswith("+++") or line.startswith("---"):
            continue
        if line.startswith("+"):
            added.append(line[1:])
        elif line.startswith("-"):
            removed.append(line[1:])
    return added, removed


def _latency_affinity(files: list[str], patch: str) -> tuple[float, str]:
    if _is_comment_only(patch):
        return 0.0, ""
    added, removed = _signed_lines(patch)
    reasons = []
    score = 0.0
    if any("prefetch_related" in ln or "select_related" in ln for ln in removed):
        score += 3
        reasons.append("removes a prefetch_related/select_related (N+1)")
    if any(
        (".annotate(" in ln or ".aggregate(" in ln or ".extra(" in ln) for ln in added
    ):
        score += 3
        reasons.append("adds a query annotation/aggregation (row explosion)")
    low = patch.lower()
    if any("/migrations/" in f for f in files) and re.search(
        r"removeindex|drop index|deleteindex|delete_index", low
    ):
        score += 3
        reasons.append("migration drops an index (seq scans)")
    return score, "; ".join(reasons)


def _search_affinity(files: list[str], patch: str) -> tuple[float, str]:
    if _is_comment_only(patch):
        return 0.0, ""
    if any("search" in f.lower() for f in files):
        return 3.0, "touches the search module"
    return 0.0, ""


def rank_frameless(
    candidates: list[dict], *, alarm_metric: str | None
) -> RankingResult:
    """Rank a silent fault's window by alarm-class diff-surface affinity, or abstain.

    Reads no ground-truth labels. Memory/5xx/connection alarms carry no code
    affinity, so they abstain (infrastructural); latency and search-canary alarms
    score the window but must clear a higher bar than the frame path."""
    if alarm_metric in INFRA_METRICS:
        return RankingResult(
            "abstain",
            "infrastructural",
            [],
            f"{ABSTAIN_INFRA_REASON} ({alarm_metric} alarm; no code surface implicated).",
        )

    if alarm_metric in LATENCY_METRICS:
        affinity = _latency_affinity
    elif alarm_metric in SEARCH_METRICS:
        affinity = _search_affinity
    else:
        return RankingResult(
            "abstain", "low_confidence", [], ABSTAIN_LOWCONF_REASON + "."
        )

    scored = []
    for c in candidates:
        files = c.get("files") or []
        patch = c.get("patch") or ""
        score, reason = affinity(files, patch)
        scored.append(
            Candidate(
                sha=c["sha"],
                score=score,
                token_hits=0,
                file_overlap=0,
                stem_overlap=0,
                blame_hits=0,
                comment_only=_is_comment_only(patch),
                files=files,
                reason=reason or "no diff-surface affinity to the alarm class",
            )
        )
    ranked = sorted(scored, key=lambda c: -c.score)
    if not ranked or ranked[0].score < _FRAMELESS_MIN:
        return RankingResult(
            "abstain", "low_confidence", ranked, ABSTAIN_LOWCONF_REASON + "."
        )
    top = ranked[0]
    return RankingResult(
        "culprit", None, ranked, f"Suspect {top.sha[:8]} (frameless): {top.reason}."
    )


def rank(
    candidates: list[dict],
    *,
    frame_files: set[str],
    tokens: set[str],
    blame_counts: dict[str, int],
    error_type: str | None,
) -> RankingResult:
    """Rank window candidates or abstain. Reads no ground-truth labels."""
    frame_files = set(frame_files or set())
    scored = [
        _score_candidate(c, frame_files, tokens, blame_counts) for c in candidates
    ]
    # Stable sort preserves compare order (oldest first) for ties -> the release
    # head (last) is never surfaced by a tiebreak.
    ranked = sorted(scored, key=lambda c: -c.score)

    strong = any(c.file_overlap or c.stem_overlap or c.blame_hits for c in scored)
    max_score = ranked[0].score if ranked else 0.0
    infra = error_type in INFRA_ERROR_TYPES

    if infra and not strong:
        reason = (
            f"{ABSTAIN_INFRA_REASON} ({error_type}; no window commit is implicated "
            "by the stack trace)."
        )
        return RankingResult("abstain", "infrastructural", ranked, reason)

    if max_score == 0:
        return RankingResult(
            "abstain", "low_confidence", ranked, ABSTAIN_LOWCONF_REASON + "."
        )

    top = ranked[0]
    reason = f"Suspect {top.sha[:8]}: {top.reason}."
    return RankingResult("culprit", None, ranked, reason)
