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
from culprit.diagnosis import build_diagnosis
from culprit.evidence import gather_evidence
from culprit.impact import compute_impact
from culprit.logparse import (
    distinct_client_ips,
    error_line_count,
    gunicorn_markers,
    log_frames,
    parse_error_events,
)
from culprit.models import Deploy, Evidence, Incident, Signal
from culprit.ranking import (
    Candidate,
    RankingResult,
    error_type_from_title,
    extract_error_tokens,
    rank,
    rank_frameless,
)
from culprit.runbooks import Runbook, load_runbooks
from culprit.similar import find_similar


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


def _alarm_metric(signals: list[Signal]) -> str | None:
    """The CloudWatch metric name of an alarm signal (drives frameless ranking)."""
    alarm = next((s for s in signals if s.source == "cloudwatch"), None)
    if alarm is None:
        return None
    trigger = ((alarm.raw or {}).get("alarm") or {}).get("Trigger") or {}
    return trigger.get("MetricName")


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
        "alarm_metric": _alarm_metric(signals),
        "log_error_count": None,
        "log_distinct_ips": None,
    }


async def _augment_from_logs(ctx: dict, logs_provider) -> None:
    """Fill frames + impact inputs from logs when the webhook carried no frames.

    Stack-trace source order (HANDOFF §4): webhook -> logs -> absent. A log-derived
    frame is normalized to the fork-relative shape the composite already ranks, so
    the silent-fault path reuses the proven ranker unchanged (plan decision 9)."""
    if (
        ctx["frames"]
        or logs_provider is None
        or not getattr(logs_provider, "enabled", False)
    ):
        return
    text = await logs_provider.read()
    if not text:
        return
    events = parse_error_events(text)
    ctx["frames"] = log_frames(events)
    ctx["log_error_count"] = error_line_count(events)
    ctx["log_distinct_ips"] = distinct_client_ips(events)
    ctx["gunicorn_markers"] = gunicorn_markers(text)


async def _resolve_deploy(session: AsyncSession, incident: Incident) -> Deploy | None:
    """The deploy that shipped this incident's window.

    Sentry incidents carry a release (the window head). A frameless SNS-only
    incident has no release, so use the most recent deploy — the one that shipped
    just before the alarm fired (the window is still pinned to a real deployed SHA)."""
    if incident.release:
        deploy = (
            await session.execute(
                select(Deploy).where(Deploy.head_sha == incident.release)
            )
        ).scalar_one_or_none()
        if deploy is not None:
            return deploy
    return (
        (
            await session.execute(
                select(Deploy).order_by(Deploy.run_started_at.desc()).limit(1)
            )
        )
        .scalars()
        .first()
    )


async def _analyze(
    session: AsyncSession, incident: Incident, *, github, logs_provider=None
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

    # Log fallback: pull frames + impact inputs from the logs when the webhook
    # carried none (a silent fault caught only by a CloudWatch alarm).
    await _augment_from_logs(ctx, logs_provider)

    deploy = await _resolve_deploy(session, incident)
    if deploy is not None and incident.release is None:
        incident.release = deploy.head_sha  # associate the frameless window's release
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

    if ctx["frames"]:
        # frame path (webhook or log frames) — the proven M2 composite, unchanged
        result = rank(
            candidates,
            frame_files={f["file"] for f in ctx["frames"] if f.get("file")},
            tokens=ctx["tokens"],
            blame_counts=blame_counts,
            error_type=ctx["error_type"],
        )
    else:
        # frameless path — alarm-class diff-surface affinity, higher abstention bar
        result = rank_frameless(candidates, alarm_metric=ctx["alarm_metric"])
    incident.verdict = result.verdict
    incident.ranked = result.as_dicts()
    await session.flush()
    return result, ctx


def _selection_context(result: RankingResult, ctx: dict) -> str:
    """The incident summary handed to the runbook selector (no ground truth)."""
    frame_files = sorted({f.get("file") for f in ctx["frames"] if f.get("file")})
    return (
        f"Incident: {ctx['title']}\n"
        f"Verdict: {result.verdict}"
        + (f" ({result.abstain_kind})" if result.abstain_kind else "")
        + "\n"
        f"Reason: {result.reason}\n"
        f"Error type: {ctx.get('error_type') or 'unknown'}\n"
        f"Stack frames: {', '.join(frame_files) or 'none'}\n"
    )


async def _offer_runbook(
    runbook_selector, result: RankingResult, ctx: dict
) -> Runbook | None:
    """Ask the (gated) selector for one runbook, resolved against the corpus."""
    if runbook_selector is None or not getattr(runbook_selector, "enabled", False):
        return None
    corpus = load_runbooks()
    rid = await runbook_selector.select_runbook(
        context=_selection_context(result, ctx), corpus=corpus
    )
    if not rid:
        return None
    # Resolve defensively: an id outside the corpus is never surfaced.
    return next((r for r in corpus if r.id == rid), None)


async def _similar_incidents(
    similar, session, incident: Incident, ctx: dict
) -> list[dict]:
    """Embed this incident (once) and cite nearest prior incidents (gated)."""
    if similar is None or not getattr(similar, "enabled", False):
        return []
    if incident.embedding is None:
        incident.embedding = await similar.embed_incident(ctx["title"], ctx["frames"])
    if incident.embedding is None:
        return []
    return await find_similar(session, list(incident.embedding), exclude_id=incident.id)


async def run_pipeline(
    session: AsyncSession,
    incident: Incident,
    *,
    github,
    llm=None,
    discord=None,
    runbook_selector=None,
    similar=None,
    logs_provider=None,
    settings=None,
) -> tuple[RankingResult, dict]:
    """Analyse, render, and post/edit the brief. Returns (result, brief payload)."""
    settings = settings or get_settings()
    result, ctx = await _analyze(
        session, incident, github=github, logs_provider=logs_provider
    )

    impact = compute_impact(
        sentry_count=ctx["count"],
        sentry_users=ctx["users"],
        log_error_count=ctx.get("log_error_count"),
        log_distinct_ips=ctx.get("log_distinct_ips"),
    )
    rationale = None
    if llm is not None and getattr(llm, "enabled", False):
        rationale = await llm.rationale(
            result, error_title=ctx["title"], impact=impact.summary()
        )

    runbook = await _offer_runbook(runbook_selector, result, ctx)

    # Ranked hypotheses citing the persisted evidence rows (plan decision 14).
    evidence_rows = (
        (
            await session.execute(
                select(Evidence).where(Evidence.incident_id == incident.id)
            )
        )
        .scalars()
        .all()
    )
    diagnosis = build_diagnosis(
        result,
        error_type=ctx["error_type"],
        evidence=[
            {"id": e.id, "commit_sha": e.commit_sha, "kind": e.kind}
            for e in evidence_rows
        ],
        runbook_id=runbook.id if runbook else None,
        impact=impact,
    )
    incident.diagnosis = diagnosis.as_dict()

    narrative = None
    if llm is not None and getattr(llm, "enabled", False):
        narrative = await llm.phrase_diagnosis(diagnosis)

    similar_matches = await _similar_incidents(similar, session, incident, ctx)

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
        impact=impact,
        diagnosis=diagnosis,
        diagnosis_narrative=narrative,
        frames=ctx["frames"],
        repo=settings.github_repo,
        resolved=incident.status == "resolved",
        runbook_id=runbook.id if runbook else None,
        runbook_title=runbook.title if runbook else None,
        runbook_summary=runbook.summary if runbook else None,
        similar=similar_matches,
    )
    payload = render_brief(brief_ctx)

    if discord is not None and getattr(discord, "enabled", False):
        if incident.brief_message_id:
            await discord.edit(incident.brief_message_id, payload)
        else:
            incident.brief_message_id = await discord.post(payload)

    await session.commit()
    return result, payload
