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

    for name, task in [
        ("up", "Task 3"),
        ("seed", "Task 2"),
        ("snapshot", "Task 2"),
        ("reset", "Task 7"),
        ("run", "Task 7"),
        ("revert", "Task 7"),
    ]:
        sp = sub.add_parser(name, help=f"{name} (implemented in {task})")
        if name == "run":
            sp.add_argument("fault_id", help="fault id from the manifest")
        sp.set_defaults(func=_not_implemented(task))

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
