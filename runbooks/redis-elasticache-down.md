---
id: redis-elasticache-down
title: Redis / ElastiCache down
summary: The Redis ElastiCache node is unreachable; because django-cachalot wraps every ORM read with no IGNORE_EXCEPTIONS, a cache outage makes near-every page 500. Infrastructural — no commit is the culprit.
failure_mode: cache-outage
symptoms:
  - ConnectionError flood across all transactions the moment the cache became unreachable
  - Errors span every page that reads the ORM, not one view — because cachalot sits in front of every read
  - The recent deploy is unrelated; timing lines up with the cache node, not a merge
checks:
  - ElastiCache node (cache.t4g.micro) health / reachability; recent failover or maintenance
  - Confirm the exception class is a Redis/cache ConnectionError, not a code exception from the window
  - Note that cachalot has no IGNORE_EXCEPTIONS, so a cache blip is not degraded-but-up — it is a hard 500
steps:
  - Check the ElastiCache node's health and recent events (failover, maintenance)
  - Restore or fail over to a healthy node and confirm the app can reach it
  - Verify pages recover as the cache comes back
  - Propose (separately, human-reviewed) setting cachalot IGNORE_EXCEPTIONS so a future blip degrades instead of 500ing
rollback: Infrastructure fault — abstain from blaming a commit. Restore the ElastiCache node (or fail over to a healthy one). The durable mitigation is setting cachalot IGNORE_EXCEPTIONS so a cache outage degrades to uncached reads instead of 500s — that is a code/config change, proposed separately.
---

# Redis / ElastiCache down

theCourseForum caches ORM reads with **django-cachalot** backed by a
**cache.t4g.micro** ElastiCache node, and cachalot is configured **without**
`IGNORE_EXCEPTIONS`. So a cache outage is not a graceful degradation — every ORM
read raises and near-every page 500s.

**Culprit offers this runbook; it never executes it.** A human performs the recovery.

## Diagnosis

- **Abstain from blaming a commit.** The `ConnectionError` flood is
  cache-connectivity, not a code regression — the "looks infrastructural" verdict.

## Fix (human-run)

1. Check the ElastiCache node's health and recent events (failover, maintenance).
2. Restore or fail over to a healthy node; confirm the app can reach it.
3. Verify pages recover as the cache comes back.

## Durable mitigation (proposed separately, human-reviewed)

- Set cachalot `IGNORE_EXCEPTIONS = True` so a future cache blip serves uncached
  reads (slower) instead of returning 500s. This is a config PR, not an incident
  action.
