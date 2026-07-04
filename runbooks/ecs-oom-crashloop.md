---
id: ecs-oom-crashloop
title: ECS task OOM / worker crash-loop
summary: The ECS Fargate task (0.5 vCPU / 2 GB) is running out of memory; gunicorn workers are SIGKILLed and requests return sporadic 502s. Often silent to Sentry because the worker dies before it can report.
failure_mode: resource-exhaustion
symptoms:
  - Sporadic 502s from the ALB with no matching Sentry event (worker killed mid-request)
  - gunicorn logs show "WORKER TIMEOUT" and workers booting/dying (SIGKILL) in a loop
  - ECS MemoryUtilization pinned near 100% of the 2 GB task limit
checks:
  - CloudWatch ECS MemoryUtilization for the task; container restart count climbing
  - gunicorn stderr for WORKER TIMEOUT / boot churn / SIGKILL markers
  - Whether a recent change raised per-request memory (a heavy query/response) vs pure traffic growth
steps:
  - Immediate — restart or scale out the ECS task to break the crash-loop
  - Durable — raise the task memory in the task definition or scale out
  - If the per-request footprint regressed with a deploy, roll that change back
  - Confirm the 502 rate and MemoryUtilization return to baseline
rollback: Relieve memory pressure — raise the task memory (task definition) or scale out tasks; if a specific endpoint's memory footprint regressed, roll that change back. Restarting the task clears the immediate crash-loop but not the cause.
---

# ECS task OOM / worker crash-loop

theCourseForum runs on a single **0.5 vCPU / 2 GB** ECS Fargate task with gunicorn
(3 workers × 2 threads). When memory is exhausted the OOM killer SIGKILLs workers
mid-request, so clients see **502s** and — critically — **Sentry often sees
nothing**, because the worker dies before it can report. This is the SNS/CloudWatch
detection case.

**Culprit offers this runbook; it never executes it.** A human performs the recovery.

## Diagnosis

- Correlate 502s with `MemoryUtilization` and gunicorn `WORKER TIMEOUT` / SIGKILL
  markers in the logs. Absence of a Sentry event is expected, not a gap.
- Decide traffic-driven (more load on a fixed task) vs regression-driven (a change
  raised per-request memory or introduced an unbounded query).

## Fix (human-run)

1. Immediate: restart / scale out the ECS task to break the crash-loop.
2. Durable: raise the task memory in the task definition, or scale out; if the
   footprint regressed with a deploy, roll that change back.
3. Confirm 502 rate and `MemoryUtilization` return to baseline.
