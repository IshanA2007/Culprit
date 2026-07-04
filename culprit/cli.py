"""``culprit`` CLI — the service's operational entrypoints.

    culprit serve      run the FastAPI app (uvicorn)
    culprit migrate    alembic upgrade head
    culprit eval       replay the M1 corpus and score (added in Task 9)

Mirrors ``harness/cli.py``'s argparse style.
"""

from __future__ import annotations

import argparse

from culprit.config import REPO_ROOT


def _alembic_config():
    from alembic.config import Config

    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    # script_location is relative to CWD in alembic.ini; pin it absolute so the
    # command works from any directory.
    cfg.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    return cfg


def _cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    uvicorn.run("culprit.app:app", host=args.host, port=args.port, reload=args.reload)
    return 0


def _cmd_migrate(args: argparse.Namespace) -> int:
    from alembic import command

    command.upgrade(_alembic_config(), args.revision)
    return 0


def _cmd_resolve(args: argparse.Namespace) -> int:
    import asyncio

    from culprit.db import get_sessionmaker
    from culprit.models import Incident
    from culprit.resolution import resolve_incident

    async def _run() -> int:
        maker = get_sessionmaker()
        async with maker() as session:
            incident = await session.get(Incident, args.incident_id)
            if incident is None:
                print(f"incident {args.incident_id} not found")
                return 1
            await resolve_incident(session, incident, source="manual")
            fix = incident.fixing_sha[:8] if incident.fixing_sha else "none (infra)"
            print(
                f"resolved incident {incident.id}: status={incident.status} "
                f"fixing_commit={fix}"
            )
            return 0

    return asyncio.run(_run())


def _cmd_eval(args: argparse.Namespace) -> int:
    import asyncio
    import json as _json

    from culprit.config import get_settings
    from culprit.db import get_sessionmaker
    from culprit.eval.driver import (
        evaluate_all,
        evaluate_runbook_precision,
        evaluate_similar_retrieval,
    )
    from culprit.eval.postmortem_eval import evaluate_postmortem_completeness
    from culprit.eval.score import (
        format_gated_sections,
        format_postmortem_section,
        format_report,
    )
    from culprit.github_api import GitHubClient
    from culprit.llm import LLM
    from culprit.similar import VoyageEmbedder

    async def _run():
        settings = get_settings()
        github = GitHubClient(settings.github_token, settings.github_repo)
        maker = get_sessionmaker()
        runbook = similar = postmortem = None
        try:
            async with maker() as session:
                agg, entries = await evaluate_all(session, github=github)
                # Postmortem completeness is deterministic (M4) — runs whenever the
                # window reads work (github token), no gated key needed.
                if settings.github_token:
                    postmortem = await evaluate_postmortem_completeness(
                        session, github=github
                    )
                # Gated sections run only with their key present (own N each).
                if not args.no_gated and settings.anthropic_api_key:
                    runbook = await evaluate_runbook_precision(
                        session, github=github, llm=LLM(settings.anthropic_api_key)
                    )
                if not args.no_gated and settings.voyage_api_key:
                    similar = await evaluate_similar_retrieval(
                        session,
                        github=github,
                        embedder=VoyageEmbedder(settings.voyage_api_key),
                    )
        finally:
            await github.aclose()
        return agg, entries, runbook, similar, postmortem

    agg, entries, runbook, similar, postmortem = asyncio.run(_run())
    if args.json:
        print(
            _json.dumps(
                {
                    "summary": agg,
                    "runs": entries,
                    "runbook": runbook,
                    "similar": similar,
                    "postmortem": postmortem,
                },
                indent=2,
                default=str,
            )
        )
    else:
        print(format_report(agg, entries))
        pm_section = format_postmortem_section(postmortem)
        if pm_section:
            print(pm_section)
        gated = format_gated_sections(runbook, similar)
        if gated:
            print(gated)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="culprit", description="Culprit service CLI")
    sub = parser.add_subparsers(dest="command", required=True)

    p_serve = sub.add_parser("serve", help="run the FastAPI app via uvicorn")
    p_serve.add_argument("--host", default="0.0.0.0")
    p_serve.add_argument("--port", type=int, default=8000)
    p_serve.add_argument("--reload", action="store_true")
    p_serve.set_defaults(func=_cmd_serve)

    p_migrate = sub.add_parser("migrate", help="alembic upgrade to a revision")
    p_migrate.add_argument("revision", nargs="?", default="head")
    p_migrate.set_defaults(func=_cmd_migrate)

    p_resolve = sub.add_parser("resolve", help="mark an incident resolved")
    p_resolve.add_argument("incident_id", type=int)
    p_resolve.set_defaults(func=_cmd_resolve)

    p_eval = sub.add_parser("eval", help="replay the M1 corpus and score")
    p_eval.add_argument(
        "--json", action="store_true", help="emit JSON instead of a table"
    )
    p_eval.add_argument(
        "--no-gated",
        action="store_true",
        help="skip the gated LLM/Voyage sections (deterministic headline only)",
    )
    p_eval.set_defaults(func=_cmd_eval)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
