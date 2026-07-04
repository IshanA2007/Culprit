"""Runbook corpus — the offer-only remediation catalog (plan decision 11).

``runbooks/*.md`` are 8–12 runbooks authored for tCF's real failure modes (their
``iac/``: RDS, ElastiCache, ECS Fargate, ALB, Cognito, CloudFront). Each is a
markdown file with a YAML frontmatter contract; this module loads + validates
the corpus (unique ids, required fields) and defines the ``RunbookSelector``
interface (the prompt-pick v1 impl lives in ``culprit/llm.py``).

**OFFER-ONLY, PERMANENTLY.** Culprit surfaces a runbook and its steps for a human
to run; it never executes one (HANDOFF §3 — the citable safety stance every GA
vendor shares). The contract carries no auto-execution affordance and the loader
rejects any runbook that declares one.

Schema-validation style mirrors ``harness/manifest.py``; self-describing flat
contracts mirror ``culprit/models.py``.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import yaml

from culprit.config import REPO_ROOT

RUNBOOKS_DIR = REPO_ROOT / "runbooks"

REQUIRED_FIELDS = (
    "id",
    "title",
    "summary",
    "failure_mode",
    "symptoms",
    "checks",
    "steps",
    "rollback",
)

# Contract keys that would imply Culprit auto-runs a step — banned permanently.
_AUTO_EXEC_KEYS = frozenset(
    {"auto_execute", "autorun", "auto_run", "execute", "run_command"}
)


class RunbookError(ValueError):
    """Raised when the runbook corpus violates a structural invariant."""


@dataclass(frozen=True)
class Runbook:
    """One offer-only runbook parsed from a ``runbooks/*.md`` frontmatter doc.

    ``frontmatter`` preserves the full parsed YAML head (audit + the offer-only
    invariant check); ``body`` is the markdown after the frontmatter block.
    """

    id: str
    title: str
    summary: str
    failure_mode: str
    symptoms: list[str]
    checks: list[str]
    steps: list[str]
    rollback: str
    frontmatter: dict[str, Any]
    body: str

    def prompt_line(self) -> str:
        """One-line ``id — title: summary`` for the selector prompt (Task 2)."""
        return f"{self.id} — {self.title}: {self.summary}"


class RunbookSelector(Protocol):
    """Chooses at most one runbook id for an incident (impl gated on the LLM key).

    Implementations MUST return an id present in ``corpus`` or ``None`` — never a
    hallucinated id (v1 constrains the model output to corpus ids, temp 0). The
    pipeline additionally resolves the returned id against the loaded corpus, so
    a stray id is dropped rather than surfaced.
    """

    enabled: bool

    async def select_runbook(
        self, *, context: str, corpus: list[Runbook]
    ) -> str | None: ...


def coerce_runbook_id(raw: str, valid_ids: set[str]) -> str | None:
    """Constrain a selector's raw output to a real corpus id (or ``None``).

    Tolerates the punctuation a model may add (backticks, a trailing period) and
    treats the explicit ``NONE`` sentinel — or any unrecognized id — as "no
    runbook fits". This is the load-bearing guard that a hallucinated id is never
    offered.
    """
    if not raw:
        return None
    token = raw.strip().strip("`").strip().rstrip(".").strip()
    if token in valid_ids:
        return token
    return None


def _parse_frontmatter(text: str, path: Path) -> tuple[dict[str, Any], str]:
    """Split a ``---``-delimited YAML frontmatter head from its markdown body."""
    if not text.startswith("---"):
        raise RunbookError(f"{path.name}: missing '---' frontmatter head")
    parts = text.split("---", 2)
    if len(parts) < 3:
        raise RunbookError(f"{path.name}: unterminated frontmatter block")
    meta = yaml.safe_load(parts[1]) or {}
    if not isinstance(meta, dict):
        raise RunbookError(f"{path.name}: frontmatter is not a mapping")
    return meta, parts[2].strip()


def _to_runbook(meta: dict[str, Any], body: str, path: Path) -> Runbook:
    missing = [f for f in REQUIRED_FIELDS if not meta.get(f)]
    if missing:
        raise RunbookError(f"{path.name}: missing required field(s) {missing}")
    return Runbook(
        id=str(meta["id"]),
        title=str(meta["title"]),
        summary=str(meta["summary"]),
        failure_mode=str(meta["failure_mode"]),
        symptoms=list(meta["symptoms"]),
        checks=list(meta["checks"]),
        steps=list(meta["steps"]),
        rollback=str(meta["rollback"]),
        frontmatter=meta,
        body=body,
    )


def load_runbooks(runbooks_dir: Path | None = None) -> list[Runbook]:
    """Parse and validate every ``runbooks/*.md`` into a sorted list of Runbooks."""
    runbooks_dir = runbooks_dir or RUNBOOKS_DIR
    runbooks: list[Runbook] = []
    for path in sorted(runbooks_dir.glob("*.md")):
        meta, body = _parse_frontmatter(path.read_text(), path)
        runbooks.append(_to_runbook(meta, body, path))
    validate(runbooks)
    return runbooks


def validate(runbooks: list[Runbook]) -> None:
    """Structural invariants: non-empty, unique ids, offer-only (no auto-exec)."""
    if not runbooks:
        raise RunbookError("no runbooks found")

    ids = [r.id for r in runbooks]
    dupes = {i for i in ids if ids.count(i) > 1}
    if dupes:
        raise RunbookError(f"duplicate runbook ids: {sorted(dupes)}")

    for r in runbooks:
        banned = _AUTO_EXEC_KEYS & set(r.frontmatter)
        if banned:
            raise RunbookError(
                f"{r.id}: offer-only violation — declares {sorted(banned)}"
            )
