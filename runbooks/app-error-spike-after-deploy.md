---
id: app-error-spike-after-deploy
title: Application error spike after a deploy
summary: A green master merge auto-deployed and a specific view/template/endpoint now throws on every request — a NoReverseMatch, FieldError, IntegrityError, or similar. Sentry floods with one dominant issue; run the culprit-commit workflow.
failure_mode: error-spike
symptoms:
  - A single dominant Sentry issue spiking immediately after a deploy (NoReverseMatch, FieldError, IntegrityError, TemplateSyntaxError, …)
  - The failing path is one view/template/endpoint, not the whole site (isolates it from infra)
  - Stack frames point at code a just-merged commit touched
checks:
  - Read the top Sentry issue's stack frames and the deploy window (Culprit brief ranks the likely culprit commit)
  - Confirm the error is a code exception, not a connectivity flood (ConnectionError/OperationalError → infra runbooks)
  - Check whether the crashing frame's file/stem/symbol matches a diff in the window
steps:
  - Take the top Sentry issue's stack frames and the deploy window
  - Rank window commits by frame-file overlap, file-stem affinity, error-named-symbol-in-diff, and blame hits (the composite Culprit computes)
  - Treat the top-ranked commit as the likely culprit; cite its diff/blame
  - Revert the culprit commit and redeploy, or roll the ECS service back to the previous task definition
  - Confirm the dominant issue stops firing before closing
rollback: Fastest path is to revert the culprit commit and redeploy, or roll the ECS service back to the previous task definition (see rollback-bad-deploy). A targeted forward hotfix is acceptable when the fix is small and obvious.
---

# Application error spike after a deploy

The common Sentry-visible case: one PR ships, a view/template/endpoint starts
throwing on every hit, and Sentry fills with a single dominant issue. Because
theCourseForum's deploy windows are usually one PR, the culprit is almost always
in the last window.

**Culprit offers this runbook; it never executes it.** A human performs the fix.

## Diagnosis (the culprit workflow)

1. Take the top Sentry issue's stack frames and the deploy window.
2. Rank window commits by frame-file overlap, file-stem affinity (e.g. a template
   `course_instructor.html` ↔ the `.py` view), error-named-symbol-in-diff, and
   blame hits — the composite Culprit already computes.
3. The top-ranked commit is the likely culprit; the brief cites its diff/blame.

## Fix (human-run)

- Revert the culprit commit and redeploy, **or** roll the ECS service back to the
  previous task definition (`rollback-bad-deploy`).
- A small, obvious forward hotfix (e.g. fixing a missed `{% url %}`) is acceptable.
- Confirm the dominant issue stops firing before closing.
