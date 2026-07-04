"""Replay one recorded run through the live pipeline (no ground truth in sight).

Per run (plan decision 5/9): reset state, seed a prior deploy at ``base_sha``,
ingest the run's deploy fixture (so ``previous_head_sha == base_sha``), ingest the
run's Sentry fixtures AND — new in M3 — its synthesized SNS alarm fixture
(silent faults open their incident here; a Sentry+SNS outage dedups cross-source),
then run the pipeline with a ``FixtureLogsProvider`` so the frameless/log-frame
path has evidence. Only the ingest contract + deploy feed reach the pipeline —
never a label. Discord/LLM are off by default so the deterministic verdict is
reproducible and free; the gated runbook/similar passes turn them on.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from culprit.cloudwatch import FixtureLogsProvider
from culprit.config import REPO_ROOT, get_settings
from culprit.correlation import correlate_signal
from culprit.deploys import reconstruct_window
from culprit.ingest.github import ingest_github
from culprit.ingest.sentry import ingest_sentry
from culprit.ingest.sns import ingest_sns_notification
from culprit.models import Base, Deploy, Incident
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
    source: str | None = None  # "sentry" | "sns" (which feed opened the incident)
    incident_count: int = 0  # incidents in the DB after replay (dedup check)
    runbook_id: str | None = None  # gated: the runbook the selector offered


async def reset_state(session: AsyncSession) -> None:
    """Truncate every service table so each run replays from a clean slate."""
    for table in reversed(Base.metadata.sorted_tables):
        await session.execute(
            text(f'TRUNCATE TABLE "{table.name}" RESTART IDENTITY CASCADE')
        )
    await session.commit()


def _log_provider(run) -> FixtureLogsProvider:
    log = run.log_paths[0] if run.log_paths else None
    return FixtureLogsProvider(REPO_ROOT / log if log else None)


async def replay_run(
    session: AsyncSession,
    run,
    *,
    github,
    llm=None,
    discord=None,
    runbook_selector=None,
    similar=None,
):
    """Replay a run record -> ReplayResult. Reads no ground-truth labels."""
    # 1. Seed the prior deploy at base_sha (decision 5) so the window base is right.
    session.add(Deploy(head_sha=run.base_sha, branch="master", run_started_at=_SEED_TS))
    await session.flush()

    # 2. Ingest the run's deploy fixture (previous_head_sha resolves to base_sha).
    deploy_env = json.loads((REPO_ROOT / run.deploy).read_text())
    await ingest_github(session, deploy_env["raw_body"].encode("latin-1"))

    window_seconds = get_settings().correlation_window_seconds

    # 3. Ingest the run's Sentry fixtures -> signals -> one incident (correlation).
    incident = None
    sentry_seen = False
    for rel in sorted(run.fixture_paths):
        if "sentry" not in rel:
            continue
        sentry_seen = True
        env = json.loads((REPO_ROOT / rel).read_text())
        signal = await ingest_sentry(
            session,
            env["raw_body"].encode("latin-1"),
            datetime.fromisoformat(env["received_at"]),
        )
        incident = await correlate_signal(session, signal, window_seconds)

    # 4. Ingest the run's SNS alarm fixture (silent faults open here; a Sentry+SNS
    #    outage dedups cross-source into the already-open incident — decision 7).
    if run.sns:
        sns_env = json.loads((REPO_ROOT / run.sns).read_text())
        notif = json.loads(sns_env["raw_body"].encode("latin-1"))
        alarm_signal = await ingest_sns_notification(
            session, notif, datetime.fromisoformat(sns_env["received_at"])
        )
        incident = await correlate_signal(session, alarm_signal, window_seconds)

    incident_count = (
        await session.execute(select(func.count(Incident.id)))
    ).scalar_one()

    if incident is None:
        # Benign baseline: no Sentry event, no alarm -> no incident (correct silence).
        return ReplayResult(
            run_id=run.run_id, produced_incident=False, incident_count=incident_count
        )

    # 5. Run the pipeline with the log provider (frameless / log-frame evidence).
    start = time.monotonic()
    result, _ = await run_pipeline(
        session,
        incident,
        github=github,
        llm=llm,
        discord=discord,
        runbook_selector=runbook_selector,
        similar=similar,
        logs_provider=_log_provider(run),
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
        source="sentry" if sentry_seen else "sns",
        incident_count=incident_count,
        runbook_id=(incident.diagnosis or {}).get("runbook_id"),
    )
