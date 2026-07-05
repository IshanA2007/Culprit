# tcf-alarms: first CloudWatch alarm suite + SNS topic + search canary

This module adds theCourseForum2's **first** production monitoring: a CloudWatch
alarm suite, an SNS topic that fans out alarm state changes over HTTPS, and a
synthetic search canary. Drop it into `iac/` as a module (or copy the `.tf`
files alongside the existing config).

## What this adds, and why

tCF runs with **zero CloudWatch alarms today** (the current `iac/cloudwatch.tf`
is two log groups). Nothing pages when latency regresses, the ALB starts
returning 5xx, RDS runs out of connections, Redis drops, or a task OOMs. This PR
closes that gap with eight alarms and one synthetic canary:

| Alarm | Metric | Catches |
|---|---|---|
| `tcf-prod-alb-target-response-time` | ALB `TargetResponseTime` p95 > 2.0s (3x60s) | N+1 / cartesian-join / dropped-index latency regressions |
| `tcf-prod-alb-5xx` | ALB `HTTPCode_ELB_5XX_Count` Sum > 5 (2x60s) | gunicorn worker OOM (502s a SIGKILLed worker never reports to Sentry) |
| `tcf-prod-alb-unhealthy-hosts` | `UnHealthyHostCount` Max > 0 (2x60s) | target group with failing health checks |
| `tcf-prod-ecs-memory-utilization` | ECS `MemoryUtilization` Avg > 85% (3x60s) | the 0.5 vCPU / 2 GB task approaching OOM |
| `tcf-prod-rds-connections` | RDS `DatabaseConnections` Max > 80 (2x60s) | the db.t3.micro connection ceiling |
| `tcf-prod-rds-cpu` | RDS `CPUUtilization` Avg > 85% (3x60s) | sustained high database CPU |
| `tcf-prod-elasticache-health` | ElastiCache `CurrConnections` Min < 1 (2x60s) | Redis node unreachable (cachalot has no `IGNORE_EXCEPTIONS`) |
| `tcf-prod-search-canary` | Synthetics `SuccessPercent` Avg < 100 (1x300s) | **silent zero-results** search fault (HTTP 200, no exception) |

The search canary is the notable one: **search silently returning zero results
is invisible to error monitoring** because it is a 200 with no exception. The
canary (`canary/python/search_smoke.py`) runs a known-good query on a schedule
and fails when the results marker is missing (or the empty-state marker
appears), which is the only way that fault surfaces as a signal.

## Offer-only posture (please read)

**Every alarm action is a notification. No alarm action mutates
infrastructure.** Both `alarm_actions` and `ok_actions` on all eight alarms
point at a single SNS topic (`tcf-prod-alarms`), and the only subscriber is an
outbound HTTPS POST to Culprit's ingest endpoint. There is no Auto Scaling
action, no Lambda remediation, no `ec2:*`/`ecs:*`/`rds:*` write anywhere in this
module. Culprit reads the notification and produces an incident brief; any
remediation is a human-approved action taken by your team, never an automated
alarm action. The `ok_actions` wiring exists purely so Culprit can auto-resolve
an incident when the metric recovers.

The SNS topic policy is least-privilege: it allows only `sns:Publish`, only from
`cloudwatch.amazonaws.com`, only for alarms in **your** account
(`aws:SourceAccount` condition), only to this one topic. The canary's IAM role is
similarly scoped (artifacts to one bucket, its own log group, and
`cloudwatch:PutMetricData` restricted to the `CloudWatchSynthetics` namespace).

## Apply-order gotcha (SNS subscription confirmation)

The HTTPS subscription to Culprit will not confirm unless Culprit is already
reachable when you apply:

1. **Deploy Culprit first** at a public HTTPS URL (Mode B self-host, ~$5/mo, or
   a Fargate service). It must be serving `POST /ingest/sns`.
2. Set `culprit_ingest_sns_url` to that URL.
3. `terraform apply`. SNS delivers a `SubscriptionConfirmation`; Culprit echoes
   the token back (`endpoint_auto_confirms = true`), and the subscription moves
   to `Confirmed` in the same apply.

If Culprit is **not** reachable at apply time, the subscription stays
`PendingConfirmation`. Alarms still fire, but nothing receives them until the
handshake completes. Re-running apply (or re-subscribing) after Culprit is up
resolves it.

## Variables to fill from your account

None of these have defaults (except the canary settings noted). Fill them from
your own infra:

| Variable | Where to get it |
|---|---|
| `culprit_ingest_sns_url` | Culprit's public `POST /ingest/sns` URL |
| `alb_arn_suffix` | `arn_suffix` of the prod ALB (e.g. `app/tcf-prod-alb/50dc6c495c0c9188`) |
| `target_group_arn_suffix` | `arn_suffix` of the prod target group |
| `ecs_cluster_name` | ECS cluster name |
| `ecs_service_name` | ECS service name |
| `rds_instance_id` | RDS `DBInstanceIdentifier` |
| `elasticache_cluster_id` | ElastiCache `CacheClusterId` |
| `search_url` | a search URL with a known-good query, e.g. `https://thecourseforum.com/search/?q=calculus` |
| `search_result_marker` | *(default `search-result`)* substring present only when results render -- **confirm against your search template** |
| `search_no_results_marker` | *(default `no-results`)* substring present only on the empty state -- **confirm against your search template** |
| `canary_schedule_expression` | *(default `rate(5 minutes)`)* canary cadence -- see cost below |

Two things worth a second look before apply:

- **Search markers.** The two marker defaults are guesses. A marker that never
  matches makes the canary always-pass (useless) or always-fail (noisy). Inspect
  your rendered search HTML and set the markers to a stable results-container
  class/id and the empty-state copy/class.
- **Canary runtime version.** `search_canary.tf` pins
  `syn-python-selenium-5.1`. AWS deprecates runtime versions over time; confirm
  it is still supported (`aws synthetics describe-runtime-versions`) and bump if
  the plan flags it.

## Cost (honest breakdown)

Standard AWS on-demand pricing, us-east-1:

| Item | Rate | Monthly |
|---|---|---|
| 8 CloudWatch alarms | ~$0.10 / alarm / month | **~$0.80** |
| Search canary @ `rate(5 minutes)` | ~$0.0012 / run, ~8,640 runs/mo | **~$10** |
| SNS HTTPS notifications | first 1M/mo effectively free at this volume | **negligible** |

The **canary is the cost driver.** If ~$10/mo is more than you want to spend on
it, drop the cadence to `rate(15 minutes)` -- roughly a third of the runs and
**~$3.50/mo** -- at the cost of detecting a silent-zero-results outage up to ~15
minutes later instead of ~5. Small artifact S3 storage and canary CloudWatch
Logs add cents.

**Total: ~$4-11/mo** depending on canary cadence. That is well below the paid
tiers of hosted error-monitoring products, and unlike those it also covers the
silent-fault class (latency regressions, silent zero-results) that error
monitoring cannot see.

## Files

| File | Contents |
|---|---|
| `versions.tf` | Terraform >= 1.5, providers aws `~> 5.0`, archive `~> 2.4` |
| `variables.tf` | All inputs (typed, described; no defaults for account-specific values) |
| `sns.tf` | Topic, HTTPS subscription, least-privilege topic policy |
| `alarms.tf` | `locals` + all 8 alarms |
| `search_canary.tf` | Artifact bucket (locked down), canary IAM role/policy, archive, canary |
| `canary/python/search_smoke.py` | The canary handler |
