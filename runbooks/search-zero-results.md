---
id: search-zero-results
title: Search returns zero / near-zero results
summary: Search silently returns empty or near-empty results with no error — a tightened similarity threshold or a broken query change. Invisible to error monitoring; only a synthetic canary or a user report catches it.
failure_mode: search-regression
symptoms:
  - Queries that used to return results now return zero or near-zero, with HTTP 200 and no exception
  - No Sentry event, no 5xx — the page renders "no results" perfectly
  - Only detectable via a search-smoke synthetic canary (known-good query must return > 0) or user complaints
checks:
  - Run a known-good query that should always match (the canary query) and confirm it returns 0
  - Check the window for a change to the TrigramSimilarity threshold or the search query/annotation
  - Confirm the trigram/GIN indexes still exist (a dropped index degrades latency, not correctness — different fault)
steps:
  - Reproduce with a known-good query that should always match (the canary query)
  - Find the window commit that changed the similarity threshold or the search query
  - Revert it or retune the threshold so real queries match again
  - Confirm the canary query returns results before closing
rollback: Restore the previous search behavior — revert the similarity-threshold change or the query change that filtered everything out. Confirm the canary query returns results again.
---

# Search returns zero / near-zero results

theCourseForum search uses Postgres **TrigramSimilarity** (no Elasticsearch). A
change that tightens the similarity threshold too far, or otherwise over-filters,
makes search return **zero results with a 200 and no exception**. Nothing in error
monitoring sees it — this is the fault class that argues for a **synthetic
canary** alarm (a known-good query that must return > 0 results).

**Culprit offers this runbook; it never executes it.** A human performs the fix.

## Diagnosis

- Reproduce with a query guaranteed to match (e.g. a common department code). If
  it returns nothing, the ranking/threshold changed — not the data.
- This is a **correctness** regression. A *latency* regression (search slow but
  correct) is a dropped index — see `perf-latency-regression`.

## Fix (human-run)

1. Find the window commit that changed the similarity threshold or search query.
2. Revert it (or retune the threshold) so real queries match again.
3. Confirm the canary query returns results before closing.

## Notes

- Without the synthetic canary this fault is effectively invisible in production;
  that canary is part of the proposed alarm suite for exactly this reason.
