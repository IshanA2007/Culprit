---
id: rds-outage-conn-exhaustion
title: RDS outage / connection exhaustion
summary: The Postgres RDS instance (db.t3.micro) is unreachable or out of connections; every request that touches the DB errors or hangs. This is infrastructural — no code commit is the culprit.
failure_mode: database-outage
symptoms:
  - OperationalError storm ("could not connect", "too many connections", "server closed the connection")
  - Silent 504 hangs if the instance is paused/failing over rather than down
  - Errors span every DB-backed page at once, unrelated to any one view or the recent deploy
checks:
  - RDS console / CloudWatch DatabaseConnections near the db.t3.micro ceiling, or the instance not "available"
  - Confirm the errors are DB-connectivity class, not a code exception from the deploy window
  - Check for a connection leak or a traffic spike exhausting the small instance's connection budget
steps:
  - Confirm instance health in the RDS console (state, recent events, failover)
  - Check DatabaseConnections against the instance limit; if exhausted, find the leak and shed load
  - Restore connectivity (resume the instance, complete the failover, or raise the connection ceiling)
  - Confirm DB-backed pages recover before closing the incident
rollback: This is an infrastructure fault — do not blame a commit. Restore the database (resume/failover the RDS instance or relieve the connection pressure); if a query pattern is leaking connections, roll back that change separately.
---

# RDS outage / connection exhaustion

theCourseForum runs Postgres on a single **db.t3.micro** RDS instance, whose
connection budget is small. When it is unreachable or saturated, near-every page
500s or hangs — this looks like a total outage but the recent deploy is innocent.

**Culprit offers this runbook; it never executes it.** A human performs the recovery.

## Diagnosis

- **Abstain from blaming a commit.** A `ConnectionError` / `OperationalError`
  flood whose frames implicate no window commit is the "No code culprit — looks
  infrastructural" case.
- Distinguish *down* (connection refused) from *exhausted* (too many connections)
  from *hung* (paused / failing over → 504s).

## Fix (human-run)

1. Confirm instance health in the RDS console (state, recent events, failover).
2. Check `DatabaseConnections` against the instance limit; if exhausted, find the
   leak (unclosed connections, a runaway query) and shed load.
3. Restore connectivity (resume the instance, complete the failover, or raise the
   connection ceiling); a larger instance class is the durable fix if the ceiling
   is chronically hit.
4. Confirm DB-backed pages recover before closing the incident.
