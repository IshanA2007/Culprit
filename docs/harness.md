# Harness Runbook

> End-to-end demo path + eval-denominator rules. Doubles as the pitch-demo
> script. **Fleshed out in Task 10** — this is the scaffold outline.

## One-command demo (target)

```bash
uv run culprit-harness up                      # boot the harness Docker profile
uv run culprit-harness run \
    template-noreversematch-instructor-card    # one code fault, multi-commit window
# -> shows the Sentry issue + captured webhook payloads
uv run culprit-harness revert                  # tear down
```

## What the harness is

See [`README.md`](../README.md) and the
[plan](../.claude/plans/culprit-m1-fault-injection-harness.plan.md).

## Prerequisites (setup state)

| Thing | Status | Notes |
|---|---|---|
| `uv` | ✅ installed | package manager |
| theCourseForum2 working clone | ⏳ `.harness-work/` | full, non-shallow checkout the harness thrashes |
| Seeded Postgres | ✅ running | `tcf_db` container, ~16k courses / ~35k reviews / `pg_trgm` present |
| Harness Docker profile | ⏳ Task 3 | `docker-compose.harness.yml` on the fork's `culprit-harness` branch |
| Sentry (free Developer) | ⛔ ask-boundary | project + internal integration + client secret |
| ngrok static domain | ⛔ ask-boundary | webhook delivery tunnel |
| GitHub fork | ⛔ ask-boundary | retains window SHAs for M2 GitHub-API reads |

## Eval-denominator rules (Task 10)

- What counts as a scoreable case: one `(fault × window-config)` pair.
- Silent faults (no Sentry webhook) are **excluded from Sentry-driven top-k
  accuracy until M3's SNS ingest exists** — their run records still carry
  culprit ground truth, so they join the eval then.
- Published N counts only scoreable cases, stated per class.
- Anti-leakage: culprit ∈ window, never == release; abstention cases carry a
  benign release; the negative control anchors the false-positive rate.

## Quota budget / tunnel setup / dump provenance

_TODO (Task 10)._
