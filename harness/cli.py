"""``culprit-harness`` command-line interface.

Subcommands mirror the plan's Task list:

  up        bring up the harness Docker profile (gunicorn :80 -> :8000, Redis)
  seed      import / refresh the local DB dump for fast resets
  snapshot  dump the current DB to db/local.dump
  reset     reset the working clone + DB to a clean harness base
  record    run the FastAPI webhook recorder
  run       run one fault scenario end-to-end (build window -> deploy -> drive)
  revert    tear down / restore after a run
  faults    list the fault catalog from the manifest

Only ``faults`` and ``record`` work without the live infra today; the rest
raise a clear NotImplementedError pointing at the plan task that lands them.
"""

from __future__ import annotations

import argparse
import sys

from harness.manifest import load_manifest


def _cmd_faults(_args: argparse.Namespace) -> int:
    faults = load_manifest()
    if not faults:
        print("No faults defined yet (harness/faults/manifest.yaml is empty).")
        return 0
    width = max(len(f.id) for f in faults)
    for f in faults:
        flags = []
        if f.requires_auth:
            flags.append("auth")
        if not f.sentry_visible:
            flags.append("silent")
        suffix = f"  [{', '.join(flags)}]" if flags else ""
        print(
            f"{f.id:<{width}}  {f.fault_class.value:<8} "
            f"gt={f.ground_truth.value:<14} signal={f.expected_signal.value}{suffix}"
        )
    print(f"\n{len(faults)} faults.")
    return 0


def _cmd_record(_args: argparse.Namespace) -> int:
    import uvicorn

    uvicorn.run("harness.recorder.app:app", host="0.0.0.0", port=_args.port)
    return 0


def _cmd_run(args: argparse.Namespace) -> int:
    import time
    from datetime import UTC, datetime

    from harness.scenarios.runner import run_scenario

    now = datetime.now(UTC)
    ts = now.strftime("%Y%m%dT%H%M%S")
    seed = args.seed if args.seed is not None else int(now.timestamp())
    rr = run_scenario(
        args.fault_id,
        size=args.size,
        culprit_position=args.position,
        timestamp=ts,
        seed=seed,
        epoch=time.time(),
    )
    print(f"\nrun record: runs/{rr.run_id}.yaml")
    print(f"  ground_truth={rr.ground_truth}  release={rr.release_sha[:10]}")
    if rr.culprit_sha:
        print(
            f"  culprit={rr.culprit_sha[:10]}  in_window={rr.culprit_in_window()}  "
            f"is_release={rr.culprit_is_release()}"
        )
    print(
        f"  window: {' -> '.join(('C' if c.is_culprit else 'd') for c in rr.window)}  (C=culprit, d=decoy)"
    )
    return 0


def _cmd_record_corpus(_args: argparse.Namespace) -> int:
    from harness.scenarios.corpus import record_all

    results = record_all()
    return 0 if all(r["status"] == "ok" for r in results) else 1


def _not_implemented(task: str):
    def _run(_args: argparse.Namespace) -> int:
        print(f"'{_args.command}' is implemented in {task} (needs live infra).")
        return 1

    return _run


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="culprit-harness", description=__doc__)
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("faults", help="list the fault catalog").set_defaults(
        func=_cmd_faults
    )

    rec = sub.add_parser("record", help="run the webhook recorder")
    rec.add_argument("--port", type=int, default=9000)
    rec.set_defaults(func=_cmd_record)

    run = sub.add_parser("run", help="run one fault scenario end-to-end")
    run.add_argument("fault_id", help="fault id from the manifest")
    run.add_argument(
        "--size", type=int, default=4, help="deploy-window size (default 4)"
    )
    run.add_argument(
        "--position",
        default="middle",
        help="culprit slot: middle/early/random (never head for size>1)",
    )
    run.add_argument("--seed", type=int, default=None)
    run.set_defaults(func=_cmd_run)

    sub.add_parser(
        "record-corpus", help="record the full corpus (all faults x window sizes)"
    ).set_defaults(func=_cmd_record_corpus)

    for name, task in [
        ("up", "Task 3"),
        ("seed", "Task 2"),
        ("snapshot", "Task 2"),
        ("reset", "Task 7"),
        ("revert", "Task 7"),
    ]:
        sp = sub.add_parser(name, help=f"{name} (implemented in {task})")
        sp.set_defaults(func=_not_implemented(task))

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
