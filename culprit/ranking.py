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
