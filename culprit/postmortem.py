"""Deterministic postmortem assembly — Culprit drafts; humans merge.

``build_postmortem`` renders the postmortem Markdown from **persisted incident
data only** (M4 plan decision 4): ``incidents.diagnosis`` (ranked hypotheses +
offered runbook + impact snapshot), the deploy/signal timeline, and the captured
``fixing_sha``. The skeleton is fixed (decision 6) and every fact is deterministic;
the LLM (Task 5) may supply the "Summary" prose only — never a SHA, a number, or a
section. slug/path/branch derive from ``opened_at`` + a sanitized title, so a
re-draft is byte-stable and the write path is idempotent.

Anti-leakage (decision 10): this module reads no ground-truth field
(``is_culprit``/``culprit_sha``/``ground_truth``). The culprit, if any, comes from
the persisted diagnosis the pipeline already computed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from culprit.impact import Impact, ImpactMetric
from culprit.models import Deploy, Incident, Postmortem, Signal
from culprit.runbooks import load_runbooks

_FOOTER = (
    "*Drafted by Culprit from persisted incident data. Culprit only offers this "
    "draft — a human reviews and merges it. Culprit never publishes unilaterally "
    "and never auto-merges.*"
)


@dataclass(frozen=True)
class PostmortemDraft:
    """A rendered postmortem + everything the GitHub App needs to open the PR."""

    slug: str
    path: str  # postmortems/YYYY-MM-DD-<slug>.md
    branch: str  # culprit/postmortem-<incident_id>-<slug>
    title: str
    body: str  # the full Markdown (frontmatter + sections)
    pr_title: str
    pr_body: str
    base: str  # the PR target branch (default branch of the fork)

    def pr_request(self) -> dict:
        """The PR-open payload (the file is written separately from path+body)."""
        return {
            "title": self.pr_title,
            "head": self.branch,
            "base": self.base,
            "body": self.pr_body,
        }


def _slugify(title: str) -> str:
    """A stable kebab-case slug from an incident title (deterministic)."""
    slug = re.sub(r"[^a-z0-9]+", "-", (title or "incident").lower()).strip("-")
    slug = "-".join(slug.split("-")[:6])  # keep it short and readable
    return slug or "incident"


def _ts(dt: datetime | None) -> str:
    return dt.strftime("%Y-%m-%d %H:%M UTC") if dt else "unknown time"


def _short(sha: str | None) -> str | None:
    return sha[:8] if sha else None


def _impact_from_snapshot(snap: dict | None) -> Impact | None:
    if not snap:
        return None

    def _m(d: dict | None) -> ImpactMetric | None:
        return ImpactMetric(d["value"], d["method"]) if d else None

    return Impact(
        failed_requests=_m(snap.get("failed_requests")),
        affected_users=_m(snap.get("affected_users")),
        window=snap.get("window"),
    )


def _culprit_sha(hypotheses: list[dict]) -> str | None:
    for h in hypotheses:
        if h.get("kind") == "code_culprit":
            return h.get("subject")
    return None


def _frontmatter(incident, hypotheses: list[dict]) -> str:
    culprit = _short(_culprit_sha(hypotheses)) or "none — infrastructural"
    fixing = _short(incident.fixing_sha) or "none — infra remediation"
    date = incident.opened_at.date().isoformat() if incident.opened_at else "unknown"
    lines = [
        "---",
        f"title: {incident.correlation_key or 'incident'}",
        f"date: {date}",
        f"incident_id: {incident.id}",
        f"severity: {incident.severity if incident.severity is not None else 'n/a'}",
        f"status: {incident.status}",
        f"culprit: {culprit}",
        f"fixing_commit: {fixing}",
        "---",
    ]
    return "\n".join(lines)


def _timeline_lines(incident, signals, deploys) -> list[str]:
    """A chronological timeline from the deploy/signal audit trail."""
    events: list[tuple[datetime, str]] = []
    by_sha = {d.head_sha: d for d in deploys}

    release_deploy = by_sha.get(incident.release) if incident.release else None
    if release_deploy is not None and release_deploy.run_started_at is not None:
        events.append(
            (
                release_deploy.run_started_at,
                f"Deploy `{_short(release_deploy.head_sha)}` shipped (the window head).",
            )
        )
    first = min(
        (s for s in signals if s.received_at is not None),
        key=lambda s: s.received_at,
        default=None,
    )
    if first is not None:
        events.append(
            (first.received_at, f"First signal ({first.source}/{first.kind}) fired.")
        )
    if incident.opened_at is not None:
        events.append(
            (incident.opened_at, "Incident opened; Culprit posted the brief.")
        )
    if incident.resolved_at is not None:
        src = incident.resolution_source or "manual"
        events.append((incident.resolved_at, f"Incident resolved (via {src})."))
    fix_deploy = by_sha.get(incident.fixing_sha) if incident.fixing_sha else None
    if fix_deploy is not None and fix_deploy.run_started_at is not None:
        events.append(
            (
                fix_deploy.run_started_at,
                f"Fix `{_short(fix_deploy.head_sha)}` shipped.",
            )
        )

    events.sort(key=lambda e: e[0])
    lines = ["## Timeline", ""]
    lines += [f"- {_ts(ts)} — {text}" for ts, text in events]
    return lines


def _rootcause_lines(hypotheses: list[dict]) -> list[str]:
    """Ranked hypotheses with confidence + cited evidence — never a single answer."""
    lines = ["## Root cause — ranked hypotheses", ""]
    if not hypotheses:
        lines.append(
            "No code culprit identified — insufficient evidence in the deploy window."
        )
        return lines
    if not any(h.get("kind") == "code_culprit" for h in hypotheses):
        lines.append("_No code culprit — looks infrastructural._ Ranked assessment:")
        lines.append("")
    for i, h in enumerate(hypotheses, 1):
        cite = (
            " (evidence "
            + ", ".join(f"#{e}" for e in h.get("evidence_ids") or [])
            + ")"
            if h.get("evidence_ids")
            else ""
        )
        lines.append(
            f"{i}. _[{h.get('confidence')} confidence]_ {h.get('statement')}{cite}"
        )
    return lines


def _resolution_lines(incident) -> list[str]:
    lines = ["## Resolution", ""]
    src = incident.resolution_source or "manual"
    if incident.fixing_sha:
        lines.append(
            f"Resolved (detected via **{src}**). Fixing commit "
            f"`{_short(incident.fixing_sha)}` shipped after the incident opened."
        )
    else:
        lines.append(
            f"Resolved (detected via **{src}**) via **infrastructure remediation** — "
            "no fixing commit; no code change shipped (an infra fault, not a code bug)."
        )
    return lines


def _runbook_lines(runbook_id: str | None) -> list[str]:
    if not runbook_id:
        return []
    runbook = next((r for r in load_runbooks() if r.id == runbook_id), None)
    if runbook is None:
        return []
    return [
        "## Suggested runbook (offered, not executed)",
        "",
        f"**{runbook.title}** — {runbook.summary}",
        "",
        "_Culprit offers this runbook; it never executes remediation._",
    ]


def _discussion_lines(thread: list[dict] | None) -> list[str]:
    """The human-narrative half — the incident chat thread (Task 4). Optional."""
    if not thread:
        return []
    lines = ["## Discussion (from the incident channel)", ""]
    for msg in thread:
        author = msg.get("author") or "unknown"
        content = (msg.get("content") or "").strip()
        if content:
            lines.append(f"- **{author}:** {content}")
    return lines


def _summary_lines(
    incident, hypotheses: list[dict], narrative: str | None
) -> list[str]:
    """The Summary — LLM prose when given (Task 5), else a deterministic fallback."""
    if narrative:
        return ["## Summary", "", narrative]
    culprit = _culprit_sha(hypotheses)
    lead = (
        f"a likely code culprit (`{_short(culprit)}`)"
        if culprit
        else "no code culprit (looks infrastructural)"
    )
    fix = (
        f"fixing commit `{_short(incident.fixing_sha)}`"
        if incident.fixing_sha
        else "infrastructure remediation (no code fix)"
    )
    return [
        "## Summary",
        "",
        f"Incident **{incident.correlation_key or incident.id}** "
        f"(severity {incident.severity if incident.severity is not None else 'n/a'}) "
        f"was diagnosed with {lead} and resolved via {fix}.",
    ]


def build_postmortem(
    *,
    incident,
    signals: list,
    deploys: list,
    repo: str,
    default_branch: str = "master",
    narrative: str | None = None,
    thread: list[dict] | None = None,
) -> PostmortemDraft:
    """Assemble the postmortem draft deterministically from persisted data."""
    diagnosis = incident.diagnosis or {}
    hypotheses = diagnosis.get("hypotheses") or []

    slug = _slugify(incident.correlation_key or f"incident-{incident.id}")
    date = incident.opened_at.date().isoformat() if incident.opened_at else "0000-00-00"
    path = f"postmortems/{date}-{slug}.md"
    branch = f"culprit/postmortem-{incident.id}-{slug}"
    title = f"Postmortem: {incident.correlation_key or f'incident #{incident.id}'}"

    impact = _impact_from_snapshot(diagnosis.get("impact"))
    impact_line = impact.render() if impact else "**Impact:** not measured"

    parts: list[str] = [
        _frontmatter(incident, hypotheses),
        "",
        f"# {title}",
        "",
        *_summary_lines(incident, hypotheses, narrative),
        "",
        "## Impact",
        "",
        impact_line,
        "",
        *_timeline_lines(incident, signals, deploys),
        "",
        *_rootcause_lines(hypotheses),
        "",
        *_resolution_lines(incident),
    ]
    runbook = _runbook_lines(diagnosis.get("runbook_id"))
    if runbook:
        parts += ["", *runbook]
    discussion = _discussion_lines(thread)
    if discussion:
        parts += ["", *discussion]
    parts += ["", "---", "", _FOOTER, ""]

    body = "\n".join(parts)
    pr_title = f"Postmortem: {incident.correlation_key or f'incident #{incident.id}'}"
    pr_body = (
        f"Culprit drafted a postmortem for incident #{incident.id}. "
        "Review and merge if it looks right — Culprit never merges on its own."
    )
    return PostmortemDraft(
        slug=slug,
        path=path,
        branch=branch,
        title=title,
        body=body,
        pr_title=pr_title,
        pr_body=pr_body,
        base=default_branch,
    )


async def draft_postmortem(
    session: AsyncSession,
    incident: Incident,
    *,
    repo: str,
    default_branch: str = "master",
    narrative: str | None = None,
    thread: list[dict] | None = None,
) -> Postmortem:
    """Assemble + persist the draft, idempotently (one ``postmortems`` row/incident).

    Loads the incident's signals and the deploy feed, renders the draft, and
    upserts the ``postmortems`` row. A re-draft updates the existing row in place
    (never a second PR for one outage).
    """
    signals = list(
        (await session.execute(select(Signal).where(Signal.incident_id == incident.id)))
        .scalars()
        .all()
    )
    deploys = list((await session.execute(select(Deploy))).scalars().all())

    draft = build_postmortem(
        incident=incident,
        signals=signals,
        deploys=deploys,
        repo=repo,
        default_branch=default_branch,
        narrative=narrative,
        thread=thread,
    )

    row = (
        await session.execute(
            select(Postmortem).where(Postmortem.incident_id == incident.id)
        )
    ).scalar_one_or_none()
    if row is None:
        row = Postmortem(
            incident_id=incident.id,
            slug=draft.slug,
            path=draft.path,
            branch=draft.branch,
            title=draft.title,
            body=draft.body,
            state="drafted",
            created_at=datetime.now(UTC),
        )
        session.add(row)
    else:  # re-draft in place — idempotent, never a second row/PR
        row.slug = draft.slug
        row.path = draft.path
        row.branch = draft.branch
        row.title = draft.title
        row.body = draft.body
    await session.commit()
    return row
