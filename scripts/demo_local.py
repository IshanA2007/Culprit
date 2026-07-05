#!/usr/bin/env python3
"""Local LIVE demo: drive one recorded incident through the full pipeline with the
real integrations turned ON — GitHub evidence, a Claude narrative, Voyage
similar-incident search, and an actual Discord post — then print the brief.

Unlike ``culprit eval`` (deterministic, integrations OFF for reproducibility), this
turns them ON. It replays a recorded fault as the trigger, so the pipeline runs
exactly as it would for a live webhook — but posts a real message to your
DISCORD_WEBHOOK_URL and makes real (cents-scale) API calls.

    uv run culprit migrate                       # once, so the dev DB has tables
    uv run python scripts/demo_local.py          # default: a Sentry-visible culprit fault
    uv run python scripts/demo_local.py --run bad-migration-drop-semester-season-w4
    uv run python scripts/demo_local.py --list   # list replayable runs

NOTE: resets the local ``culprit`` dev DB (TRUNCATE) before replaying, like the eval.
"""

from __future__ import annotations

import argparse
import asyncio

from sqlalchemy import select

from culprit.brief import DiscordClient
from culprit.config import get_settings
from culprit.db import get_sessionmaker
from culprit.eval.replay import replay_run, reset_state
from culprit.github_api import GitHubClient
from culprit.llm import LLM
from culprit.models import Incident
from culprit.similar import SimilarIncidentSearch, VoyageEmbedder
from harness.runrecord import load_all_run_records


def _pick(runs, needle: str):
    match = next((r for r in runs if needle in r.run_id), None)
    if match is None:
        raise SystemExit(f"no run matches {needle!r} — try --list")
    return match


async def _run(needle: str) -> None:
    settings = get_settings()
    run = _pick(load_all_run_records(), needle)

    github = GitHubClient(settings.github_token, settings.github_repo)
    llm = LLM(settings.anthropic_api_key)
    discord = DiscordClient(settings.discord_webhook_url)
    similar = SimilarIncidentSearch(VoyageEmbedder(settings.voyage_api_key))

    print(f"\n▶ replaying {run.run_id}  (ground truth: {run.ground_truth})")
    print(
        f"  live integrations — discord:{'ON' if discord.enabled else 'off'} "
        f"llm:{'ON' if getattr(llm, 'enabled', False) else 'off'} "
        f"github:{'token' if settings.github_token else 'anon'} "
        f"voyage:{'ON' if getattr(similar, 'enabled', False) else 'off'}"
    )

    maker = get_sessionmaker()
    try:
        async with maker() as session:
            await reset_state(session)
            result = await replay_run(
                session,
                run,
                github=github,
                llm=llm,
                discord=discord,
                runbook_selector=llm,
                similar=similar,
            )
            incident = (
                await session.execute(
                    select(Incident).order_by(Incident.id.desc()).limit(1)
                )
            ).scalar_one_or_none()

        print(f"\n── verdict: {result.verdict.upper()} ──")
        for i, c in enumerate((incident.ranked if incident else [])[:3], 1):
            print(f"  {i}. {c['sha'][:10]}  score={c['score']}  {c.get('reason', '')}")
        if run.culprit_sha:
            hit1 = result.ranked[:1] == [run.culprit_sha]
            hit3 = run.culprit_sha in result.ranked[:3]
            print(
                f"\n  ground-truth culprit {run.culprit_sha[:10]} -> "
                f"top-1 {'✓' if hit1 else '✗'}   top-3 {'✓' if hit3 else '✗'}"
            )
        diag = (incident.diagnosis or {}) if incident else {}
        if diag.get("runbook_id"):
            print(f"  runbook offered: {diag['runbook_id']}")
        if discord.enabled:
            print("\n  ✅ live brief posted to your Discord channel — go look.")
    finally:
        await github.aclose()
        await discord.aclose()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--run", default="search-fielderror-500-w4", help="run-id substring")
    ap.add_argument("--list", action="store_true", help="list replayable runs and exit")
    args = ap.parse_args()
    if args.list:
        for r in load_all_run_records():
            print(f"{r.run_id}  ({r.ground_truth})")
        return
    asyncio.run(_run(args.run))


if __name__ == "__main__":
    main()
