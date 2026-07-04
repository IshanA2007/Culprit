# Culprit — Fault-Injection Harness (Milestone 1)

The harness that comes **before** the service. It forks
[theCourseForum2](https://github.com/thecourseforum/theCourseForum2), runs it
locally in a production-faithful Docker profile, wires Sentry, injects ~12
labeled faults on interleaved multi-commit deploy windows, and records the real
Sentry / GitHub webhook payloads as a labeled eval corpus.

That corpus is three things at once:

1. **The demo** — theCourseForum's incident rate is low, so we manufacture
   incidents on a fork of their real code.
2. **The eval source** — every resume number (top-1/top-3 culprit accuracy,
   abstention rate, false-positive rate) is computed from these labeled runs.
3. **The ingest contract** — the recorder that captures webhooks grows into
   Milestone 2's ingest service, so the fixture format *is* the `POST /ingest/*`
   contract.

See [`.claude/plans/culprit-m1-fault-injection-harness.plan.md`](.claude/plans/culprit-m1-fault-injection-harness.plan.md)
for the full plan and [`docs/harness.md`](docs/harness.md) for the runbook.

## Quickstart

```bash
uv sync
uv run ruff check .
uv run pytest
uv run culprit-harness --help
```

## Layout

| Path | What |
|---|---|
| `harness/` | The harness engine — CLI, deploy-window builder, decoys, Sentry release wrapper, traffic driver, scenario runner |
| `harness/recorder/` | FastAPI catch-all that dumps raw webhook payloads to `fixtures/` |
| `harness/faults/` | Fault diffs (`*.patch`) + `manifest.yaml` (labeled ground truth) |
| `harness/scenarios/` | The orchestrated run (build window → deploy → drive traffic → record) |
| `fixtures/` | The recorded corpus (raw Sentry/GitHub payloads + sidecar labels) |
| `runs/` | Per-scenario run records (base SHA, window commits, ground truth) |
| `.harness-work/` | Gitignored full checkout of theCourseForum2 the harness thrashes |

## Status

Milestone 1, in progress. This repo is greenfield; conventions are mirrored from
theCourseForum2's toolchain (uv / ruff / pytest / GitHub Actions).
