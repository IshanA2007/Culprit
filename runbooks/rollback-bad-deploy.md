---
id: rollback-bad-deploy
title: Roll back a bad deploy
summary: A recent master merge auto-deployed and broke production; revert the ECS service to the previous task definition / image while the culprit commit is reverted in git.
failure_mode: bad-deploy
symptoms:
  - New 500s or crashes that started immediately after an "AWS Deployment" workflow_run went green
  - Errors trace to view/template/import code that a just-merged commit touched
  - /health still returns "ok" (middleware short-circuits before view resolution) so ALB shows healthy while pages 500
checks:
  - Confirm the incident window opened within minutes of the latest deploy (deploy timeline / Culprit brief)
  - Compare the deployed SHA against the previous stable SHA (compare view in the brief)
  - Check the ECS service (barrett-fogle-love-v1) event log for the release that shipped the culprit image
steps:
  - Identify the last-known-good ECS task definition revision and the ECR image tag it points at
  - Update the ECS service barrett-fogle-love-v1 to that previous revision and wait for the service to stabilize
  - Revert the culprit commit on master (a git revert PR) so the next deploy does not re-ship the regression
  - Confirm error rates return to baseline before closing the incident
rollback: Update the ECS service to the previous task definition revision (previous ECR image tag) and wait for the service to reach a steady state; this is faster than a forward fix and restores the last-known-good image.
---

# Roll back a bad deploy

theCourseForum's CD (`aws.yml`) auto-deploys **every green master merge**: build →
ECR → a one-off ECS release task → update task definition → wait for stability.
Deploy windows are usually one PR, so a fresh regression almost always traces to
the commit(s) in the last window.

**Culprit offers this runbook; it never executes it.** A human performs the rollback.

## Fix (human-run)

1. Identify the last-known-good ECS **task definition revision** (the one before
   the current release) and the ECR image tag it points at.
2. Update the ECS service `barrett-fogle-love-v1` to that previous task definition
   revision and wait for the service to stabilize (ALB targets healthy).
3. Revert the culprit commit on `master` (a `git revert` PR) so the next deploy
   does not re-ship the regression.
4. Confirm error rates return to baseline in the brief before closing the incident.

## Notes

- Prefer rollback over a forward hotfix when the blast radius is site-wide — it is
  the fastest path back to a known-good image.
- If the deploy also ran a **migration** (release task runs `migrate`), a plain
  image rollback may leave schema skew — see `bad-migration-rollback`.
