"""Drive the whole corpus through replay + scoring.

The deterministic pass (``evaluate_all``) is LLM-free and reproducible — the
headline M3 numbers. The gated passes score the LLM/Voyage sections separately,
each with its own N (plan decision 12/13), and never touch the deterministic
verdict.
"""

from __future__ import annotations

from collections import defaultdict

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from culprit.eval.replay import replay_run, reset_state
from culprit.eval.score import aggregate, load_runbook_labels, score_run
from culprit.models import Incident, Signal
from culprit.similar import find_similar, incident_text
from harness.runrecord import load_all_run_records


async def evaluate_all(
    session: AsyncSession, *, github, runs=None
) -> tuple[dict, list[dict]]:
    """Replay every run (resetting state between them) and score. Returns (agg, entries)."""
    runs = runs if runs is not None else load_all_run_records()
    entries: list[dict] = []
    for run in runs:
        await reset_state(session)
        replay = await replay_run(session, run, github=github)
        entries.append(score_run(run, replay))
    return aggregate(entries), entries


async def evaluate_runbook_precision(
    session: AsyncSession, *, github, llm, runs=None
) -> dict:
    """GATED (LLM): re-replay with the selector; score the offered runbook vs the
    scorer-only labels. The pipeline never sees fault_id (anti-leakage)."""
    runs = runs if runs is not None else load_all_run_records()
    labels = load_runbook_labels()
    rows = []
    for run in runs:
        await reset_state(session)
        replay = await replay_run(session, run, github=github, runbook_selector=llm)
        if not replay.produced_incident:
            continue
        expected = labels.get(run.fault_id)
        rows.append(
            {
                "run_id": run.run_id,
                "expected": expected,
                "selected": replay.runbook_id,
                "correct": replay.runbook_id == expected,
            }
        )
    return {
        "n": len(rows),
        "correct": sum(1 for r in rows if r["correct"]),
        "rows": rows,
    }


async def _incident_text_for(session: AsyncSession, run, *, github) -> str | None:
    """Replay a run LLM/Voyage-free and return the text the pipeline would embed."""
    await reset_state(session)
    replay = await replay_run(session, run, github=github)
    if not replay.produced_incident:
        return None
    incident = (
        await session.execute(select(Incident).order_by(Incident.id.desc()).limit(1))
    ).scalar_one()
    signals = (
        (await session.execute(select(Signal).where(Signal.incident_id == incident.id)))
        .scalars()
        .all()
    )
    frames = next((s.frames for s in signals if s.frames), [])
    return incident_text(incident.correlation_key or "", frames)


async def evaluate_similar_retrieval(
    session: AsyncSession, *, github, embedder, runs=None
) -> dict:
    """GATED (Voyage/pgvector): for each fault's w1/w4 sibling pair, does the second
    retrieve its OWN first among all prior incidents? The texts are batch-embedded
    in ONE Voyage call (free-tier rate-limit friendly), then retrieved via pgvector.
    Frameless faults that share an identical alarm text collide — reported honestly."""
    runs = runs if runs is not None else load_all_run_records()
    by_fault: dict[str, list] = defaultdict(list)
    for run in runs:
        by_fault[run.fault_id].append(run)

    pairs_texts: list[tuple[str, str]] = []
    for group in by_fault.values():
        siblings = sorted(group, key=lambda r: r.run_id)
        if len(siblings) < 2:
            continue
        t1 = await _incident_text_for(session, siblings[0], github=github)
        t2 = await _incident_text_for(session, siblings[1], github=github)
        if t1 and t2:
            pairs_texts.append((t1, t2))

    if not pairs_texts:
        return {"n": 0, "correct": 0}

    # ONE batch embed for every text (2 per pair) — minimizes Voyage calls.
    flat = [t for pair in pairs_texts for t in pair]
    vectors = await embedder.embed(flat)
    if not vectors:
        return {"n": 0, "correct": 0}

    # Seed all "first" incidents, then check each "second" retrieves its own first
    # from the whole prior-incident set.
    await reset_state(session)
    first_ids: list[int] = []
    for i in range(len(pairs_texts)):
        inc = Incident(
            status="resolved",
            correlation_key=f"pair-{i}-first",
            embedding=vectors[2 * i],
        )
        session.add(inc)
        await session.flush()
        first_ids.append(inc.id)
    await session.commit()

    hits = 0
    for i in range(len(pairs_texts)):
        matches = await find_similar(
            session, vectors[2 * i + 1], exclude_id=-1, limit=1
        )
        if matches and matches[0]["id"] == first_ids[i]:
            hits += 1

    return {"n": len(pairs_texts), "correct": hits}
