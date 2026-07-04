---
id: alb-5xx-triage
title: ALB 5xx spike triage
summary: The load balancer is reporting a spike of 5xx responses; triage whether the fault is the target (app 500s), the target being unreachable (502/503/504), or the ELB itself, then route to the specific runbook.
failure_mode: http-5xx-spike
symptoms:
  - CloudWatch ALB HTTPCode_ELB_5XX_Count or HTTPCode_Target_5XX_Count spiking
  - Users report the site is down or erroring broadly
  - UnHealthyHostCount may be > 0 if targets are failing health checks
checks:
  - Split ELB 5xx (LB-generated, e.g. no healthy target) from Target 5xx (app returned 500)
  - Check UnHealthyHostCount and target health — a dead task is 502/503, an app bug is Target 5xx / 500
  - Correlate the spike start with the latest deploy vs an infra event (DB/cache/OOM)
steps:
  - Classify the 5xx sub-type (ELB-generated vs Target) and read UnHealthyHostCount
  - Target 5xx with healthy targets and a recent deploy → route to rollback-bad-deploy / app-error-spike-after-deploy
  - ELB 5xx (502/503) with unhealthy targets and gunicorn SIGKILL/OOM → route to ecs-oom-crashloop
  - Broad connectivity errors across every page → route to rds-outage-conn-exhaustion or redis-elasticache-down by error class
rollback: This runbook triages, it does not itself remediate — hand off to rollback-bad-deploy (app 500s after a deploy), ecs-oom-crashloop (502s + OOM), rds-outage-conn-exhaustion or redis-elasticache-down (infra floods) based on the 5xx class.
---

# ALB 5xx spike triage

A 5xx spike on the ALB is a symptom, not a cause. The value of this runbook is
**routing**: the sub-type of 5xx and the target health tell you which specific
runbook to open.

**Culprit offers this runbook; it never executes it.** A human performs the triage.

## Triage decision

1. **Target 5xx / app 500s, targets healthy** → the app is raising exceptions.
   If it started right after a deploy, go to `rollback-bad-deploy` /
   `app-error-spike-after-deploy`.
2. **ELB 5xx (502/503) with UnHealthyHostCount > 0** → targets are dying or
   unreachable. If gunicorn shows SIGKILL/OOM, go to `ecs-oom-crashloop`.
3. **Broad connectivity errors across every page** → infrastructure. Go to
   `rds-outage-conn-exhaustion` or `redis-elasticache-down` per the error class.

## Notes

- `/health` returning "ok" while pages 500 means the app process is up but a view
  path is broken — that points at code, not infra.
