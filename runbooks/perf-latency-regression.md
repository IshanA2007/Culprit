---
id: perf-latency-regression
title: Performance / latency regression
summary: A page or query got dramatically slower after a change — an N+1, a cartesian join, or lost indexes — pushing latency up and sometimes tripping the gunicorn worker timeout (502/504). No exception; the symptom is time.
failure_mode: latency-regression
symptoms:
  - ALB TargetResponseTime p95 climbing; specific pages slow, others fine
  - Worker-timeout 502/504 under the 120s gunicorn timeout when a query explodes
  - Extra queries per request (N+1) or a row-exploding join, or sequential scans where an index used to serve the query
checks:
  - EXPLAIN the slow query; look for seq scans that should be index scans (dropped GIN/trigram index) or a row-multiplying join
  - Check the window for a change to select_related/prefetch_related, a new annotate/join, or a migration dropping an index
  - Confirm results are still correct (rules out a logic bug) — this is purely a performance change
steps:
  - Identify the slow endpoint from TargetResponseTime / worker-timeout logs
  - EXPLAIN the query — seq scan where an index used to serve it, a row-multiplying join, or an extra query per row
  - Map the regression to a window commit (removed prefetch_related, combined annotate, or migration dropping an index)
  - Apply the fix — re-add the dropped index, restore prefetch_related/select_related, or split the combined annotation
  - Roll back the culprit commit and confirm p95 latency returns to baseline
rollback: Restore the fast query path — re-add the dropped index (a corrective migration), restore the prefetch_related/select_related, or split the combined annotation that caused the cartesian join. Roll back the specific commit that changed the query shape.
---

# Performance / latency regression

The dangerous performance faults on theCourseForum are query-shape regressions:
an inlined section lookup that becomes an **N+1**, two annotations combined into
one query that becomes a **cartesian join** (row explosion → blows the 120s
gunicorn timeout → 502/504), or a migration that **drops the trigram/GIN search
indexes** so search falls back to sequential scans (10–100× slower). Results stay
correct, so there is no exception — only latency.

**Culprit offers this runbook; it never executes it.** A human performs the fix.

## Diagnosis

1. Identify the slow endpoint from `TargetResponseTime` / worker-timeout logs.
2. `EXPLAIN` the query: seq scan where an index used to serve it? A join whose row
   count is the product of two sets? An extra query per row?
3. Map it to a window commit — a removed `prefetch_related`, a combined
   `annotate`, or a migration dropping an index.

## Fix (human-run)

- **Lost index** → corrective migration re-adding the GIN/trigram index.
- **N+1** → restore `select_related`/`prefetch_related`.
- **Cartesian join** → split the combined annotation into separate queries.
- Roll back the specific commit that changed the query shape and confirm p95
  latency returns to baseline.
