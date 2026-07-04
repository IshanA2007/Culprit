"""Replay one recorded run through the live pipeline (no ground truth in sight).

Per run (plan decision 5/9): reset state, seed a prior deploy at ``base_sha``,
ingest the run's deploy fixture (so ``previous_head_sha == base_sha``), ingest the
run's Sentry fixtures (-> signals -> one incident via correlation), then run the
pipeline. Only ``base_sha``/``release_sha``/the deploy fixture/the Sentry fixtures
reach the pipeline — never a label. Discord/LLM are off so the eval is
reproducible and free (the deterministic verdict is what's scored).
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from culprit.config import REPO_ROOT, get_settings
from culprit.correlation import correlate_signal
from culprit.deploys import reconstruct_window
from culprit.ingest.github import ingest_github
from culprit.ingest.sentry import ingest_sentry
from culprit.models import Base, Deploy
from culprit.pipeline import run_pipeline

# A deterministic "old" timestamp for the seeded prior deploy (before every run).
_SEED_TS = datetime(2020, 1, 1, tzinfo=UTC)


@dataclass
class ReplayResult:
    run_id: str
    produced_incident: bool
    verdict: str | None = None
    ranked: list[str] = field(default_factory=list)  # candidate SHAs, best first
    window: list[str] = field(default_factory=list)  # reconstructed window SHAs
    time_to_brief_s: float | None = None


async def reset_state(session: AsyncSession) -> None:
    """Truncate every service table so each run replays from a clean slate."""
    for table in reversed(Base.metadata.sorted_tables):
        await session.execute(
            text(f'TRUNCATE TABLE "{table.name}" RESTART IDENTITY CASCADE')
        )
    await session.commit()


async def replay_run(session: AsyncSession, run, *, github, llm=None, discord=None):
    """Replay a run record -> ReplayResult. Reads no ground-truth labels."""
    # 1. Seed the prior deploy at base_sha (decision 5) so the window base is right.
    session.add(Deploy(head_sha=run.base_sha, branch="master", run_started_at=_SEED_TS))
    await session.flush()

    # 2. Ingest the run's deploy fixture (previous_head_sha resolves to base_sha).
    deploy_env = json.loads((REPO_ROOT / run.deploy).read_text())
    await ingest_github(session, deploy_env["raw_body"].encode("latin-1"))

    # 3. Ingest the run's Sentry fixtures -> signals -> one incident (correlation).
    window_seconds = get_settings().correlation_window_seconds
    incident = None
    for rel in sorted(run.fixture_paths):
        if "sentry" not in rel:
            continue
        env = json.loads((REPO_ROOT / rel).read_text())
        signal = await ingest_sentry(
            session,
            env["raw_body"].encode("latin-1"),
            datetime.fromisoformat(env["received_at"]),
        )
        incident = await correlate_signal(session, signal, window_seconds)

    if incident is None:
        # Silent fault or benign baseline: no Sentry signal -> no incident.
        return ReplayResult(run_id=run.run_id, produced_incident=False)

    # 4. Run the pipeline (deterministic verdict; brief not posted in eval).
    start = time.monotonic()
    result, _ = await run_pipeline(
        session, incident, github=github, llm=llm, discord=discord
    )
    elapsed = time.monotonic() - start

    # Reconstruct the window independently for the fidelity check (cached; no labels).
    window = await reconstruct_window(github, run.base_sha, run.release_sha)

    return ReplayResult(
        run_id=run.run_id,
        produced_incident=True,
        verdict=result.verdict,
        ranked=[c.sha for c in result.ranked],
        window=[c["sha"] for c in window],
        time_to_brief_s=elapsed,
    )
