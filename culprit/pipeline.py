"""The analysis loop — incident -> window -> evidence -> rank -> brief.

Deterministic and idempotent: the first call analyses (reconstructs the window,
gathers evidence, ranks) and posts the brief; later calls (a new signal joined)
re-render with updated impact and *edit* the living message. All culprit reads
are pinned to the deployed SHA; the LLM only phrases (deterministic authoritative).
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from culprit.brief import BriefContext, render_brief
from culprit.config import get_settings
from culprit.deploys import reconstruct_window
from culprit.evidence import gather_evidence
from culprit.models import Deploy, Incident, Signal
from culprit.ranking import (
    Candidate,
    RankingResult,
    error_type_from_title,
    extract_error_tokens,
    rank,
)


async def _signals(session: AsyncSession, incident_id: int) -> list[Signal]:
    return list(
        (await session.execute(select(Signal).where(Signal.incident_id == incident_id)))
        .scalars()
        .all()
    )


def _error_context(signal: Signal | None) -> tuple[set[str], str | None, str | None]:
    if signal is None:
        return set(), None, None
    title = signal.fingerprint
    error_type = error_type_from_title(title or "")
    data = (signal.raw or {}).get("data", {})
    event = data.get("event") or data.get("issue") or {}
    meta = event.get("metadata") or {}
    if meta.get("type"):
        error_type = meta.get("type")
    tokens = extract_error_tokens(title, meta.get("value"))
    return tokens, error_type, title


async def _context(session: AsyncSession, incident: Incident) -> dict:
    signals = await _signals(session, incident.id)
    event_signal = next(
        (s for s in signals if s.frames), signals[0] if signals else None
    )
    tokens, error_type, title = _error_context(event_signal)
    return {
        "frames": event_signal.frames if event_signal else [],
        "tokens": tokens,
        "error_type": error_type,
        "title": title or incident.correlation_key or "incident",
        "count": max((s.count or 0 for s in signals), default=0),
        "users": max((s.users or 0 for s in signals), default=0),
    }


async def _analyze(
    session: AsyncSession, incident: Incident, *, github
) -> tuple[RankingResult, dict]:
    ctx = await _context(session, incident)

    if incident.verdict:  # already analysed — rebuild the result from stored fields
        ranked = [
            Candidate(
                sha=c["sha"],
                score=c["score"],
                token_hits=0,
                file_overlap=0,
                stem_overlap=0,
                blame_hits=0,
                comment_only=False,
                reason=c.get("reason", ""),
            )
            for c in (incident.ranked or [])
        ]
        reason = f"Suspect {ranked[0].sha[:8]}: {ranked[0].reason}" if ranked else ""
        kind = None if incident.verdict == "culprit" else "infrastructural"
        return RankingResult(incident.verdict, kind, ranked, reason), ctx

    deploy = (
        await session.execute(select(Deploy).where(Deploy.head_sha == incident.release))
    ).scalar_one_or_none()
    window = (
        await reconstruct_window(github, deploy.previous_head_sha, deploy.head_sha)
        if deploy
        else []
    )
    window_shas = [c["sha"] for c in window]

    evidence = (
        await gather_evidence(
            session,
            github,
            incident_id=incident.id,
            window_shas=window_shas,
            frames=ctx["frames"],
            release_sha=incident.release,
        )
        if window_shas
        else []
    )

    window_set = set(window_shas)
    candidates = [
        {
            "sha": e.commit_sha,
            "files": e.payload.get("files", []),
            "patch": e.payload.get("patch", ""),
        }
        for e in evidence
        if e.kind == "diff"
    ]
    blame_counts: dict[str, int] = {}
    for e in evidence:
        if e.kind == "blame" and e.commit_sha in window_set:
            blame_counts[e.commit_sha] = blame_counts.get(e.commit_sha, 0) + 1

    result = rank(
        candidates,
        frame_files={f["file"] for f in ctx["frames"] if f.get("file")},
        tokens=ctx["tokens"],
        blame_counts=blame_counts,
        error_type=ctx["error_type"],
    )
    incident.verdict = result.verdict
    incident.ranked = result.as_dicts()
    await session.flush()
    return result, ctx


async def run_pipeline(
    session: AsyncSession,
    incident: Incident,
    *,
    github,
    llm=None,
    discord=None,
    settings=None,
) -> tuple[RankingResult, dict]:
    """Analyse, render, and post/edit the brief. Returns (result, brief payload)."""
    settings = settings or get_settings()
    result, ctx = await _analyze(session, incident, github=github)

    impact = f"~{ctx['count']} failed requests" + (
        f", ≈{ctx['users']} users" if ctx["users"] else ""
    )
    rationale = None
    if llm is not None and getattr(llm, "enabled", False):
        rationale = await llm.rationale(result, error_title=ctx["title"], impact=impact)

    brief_ctx = BriefContext(
        title=ctx["title"],
        verdict=result.verdict,
        abstain_kind=result.abstain_kind,
        reason=result.reason,
        ranked=result.as_dicts(),
        rationale=rationale,
        release=incident.release,
        count=ctx["count"],
        users=ctx["users"],
        frames=ctx["frames"],
        repo=settings.github_repo,
        resolved=incident.status == "resolved",
    )
    payload = render_brief(brief_ctx)

    if discord is not None and getattr(discord, "enabled", False):
        if incident.brief_message_id:
            await discord.edit(incident.brief_message_id, payload)
        else:
            incident.brief_message_id = await discord.post(payload)

    await session.commit()
    return result, payload
