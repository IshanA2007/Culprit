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


def _cmd_eval(args: argparse.Namespace) -> int:
    import asyncio
    import json as _json

    from culprit.config import get_settings
    from culprit.db import get_sessionmaker
    from culprit.eval.driver import evaluate_all
    from culprit.eval.score import format_report
    from culprit.github_api import GitHubClient

    async def _run() -> tuple[dict, list[dict]]:
        settings = get_settings()
        github = GitHubClient(settings.github_token, settings.github_repo)
        maker = get_sessionmaker()
        try:
            async with maker() as session:
                return await evaluate_all(session, github=github)
        finally:
            await github.aclose()

    agg, entries = asyncio.run(_run())
    if args.json:
        print(_json.dumps({"summary": agg, "runs": entries}, indent=2, default=str))
    else:
        print(format_report(agg, entries))
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

    p_eval = sub.add_parser("eval", help="replay the M1 corpus and score")
    p_eval.add_argument(
        "--json", action="store_true", help="emit JSON instead of a table"
    )
    p_eval.set_defaults(func=_cmd_eval)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
