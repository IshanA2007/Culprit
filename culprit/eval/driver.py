"""Drive the whole corpus through replay + scoring."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from culprit.eval.replay import replay_run, reset_state
from culprit.eval.score import aggregate, score_run
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
