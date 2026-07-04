---
id: bad-migration-rollback
title: Recover from a bad migration
summary: A migration that shipped in the release task changed the schema in a way the deployed code (or a halted deploy) can't tolerate; restore schema/code compatibility.
failure_mode: bad-migration
symptoms:
  - ProgrammingError / undefined column / relation errors on pages that were fine before the deploy
  - Errors begin only after the release task's `migrate` step ran, not at image swap
  - A recently merged migration file drops or renames a column/index the running code still SELECTs
checks:
  - Confirm a new migration file is in the deploy window (compare view)
  - Check whether the release task's `migrate` completed while the app rollout halted (migrate-applied-but-code-old skew)
  - Identify the column/index/table the failing query references and the migration that changed it
steps:
  - Determine the skew direction (schema ahead of code, or code ahead of schema)
  - If destructive and the code needs the old schema, apply the migration's reverse or a corrective forward migration re-adding the column/index
  - Redeploy the image whose code matches the intended schema
  - Verify the previously-failing page returns 200 before closing
rollback: Re-apply the compatible schema by reversing the offending migration (or rolling forward a corrective migration), then align the running image to the schema; if code and schema are skewed, restore the pair that is mutually compatible.
---

# Recover from a bad migration

The release task runs `migrate && collectstatic && invalidate_cachalot &&
clearsessions` **before** the new task definition is fully healthy. A migration
that drops or renames a column the running code still reads (e.g. dropping
`Semester.season`, which `Semester.latest()` SELECTs in a context processor on
every HTML page) makes pages 500 as soon as `migrate` applies — even if the code
rollout itself stalls.

**Culprit offers this runbook; it never executes it.** A human performs the recovery.

## Fix (human-run)

1. Determine the skew direction: did the schema move ahead of the code (migration
   applied, old image still serving) or the reverse?
2. If the migration is destructive and the code needs the old schema, roll the
   schema back by applying the migration's reverse (or a corrective forward
   migration that re-adds the column/index).
3. Redeploy the image whose code matches the intended schema.
4. Verify the previously-failing page (e.g. a course-instructor page) returns 200.

## Notes

- Destructive migrations (drop column/index) are the dangerous class here because
  the CD pipeline separates `migrate` from a healthy rollout.
- If the migration dropped an **index** and the symptom is *latency* rather than
  an error, treat it as a performance regression — see `perf-latency-regression`.
