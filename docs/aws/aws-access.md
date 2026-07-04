# Culprit — the AWS access ask (for theCourseForum's VP of Infra)

Culprit needs **read-only** AWS access to turn the synthesized-fixture demo into
live incident response. This is the single most likely stall point (HANDOFF §7
Q2), so the ask is scoped as tightly as possible, and **nothing in Milestones 1–3
depends on it** — the whole pipeline is proven on shape-faithful fixtures today.

## What we ask for

A read-only IAM role/user with **exactly** the policy in
[`culprit-readonly-policy.json`](culprit-readonly-policy.json):

| Scope | Why | Read-only? |
|---|---|---|
| `logs:StartQuery`/`GetQueryResults`/`FilterLogEvents` on the **two** log groups (`/ecs/tcf-prod-django`, `redis-cache`) | The middleware already dumps exception JSON + gunicorn markers here — Culprit's frameless/log-frame evidence (`FixtureLogsProvider` → `Boto3LogsProvider`) | Yes |
| `cloudwatch:GetMetricData`/`DescribeAlarms` | Exact request/5xx/latency counts for impact; alarm state for triage | Yes |
| `ecs:Describe*`/`ListTasks` | Task health / memory for the OOM crash-loop case | Yes |
| `elasticloadbalancing:Describe*` | ALB 5xx / target health for triage | Yes |

`ACCOUNT_ID` and the exact log-group names are placeholders to fill from their
account. **No write, no delete, no mutate** — consistent with the offer-only
stance (Culprit never executes remediation).

## The second pitch PR: the alarm suite

tCF has **zero alarms today**. [`alarms-proposal.tf`](alarms-proposal.tf) is the
CloudWatch alarm suite Culprit proposes (ALB latency/5xx/unhealthy-hosts, ECS
memory, RDS connections/CPU, ElastiCache health, and a **synthetic search
canary** — the only detector for the silent zero-results fault), in their `iac/`
style, publishing to an SNS topic subscribed to `POST /ingest/sns`. It is both the
instrumentation they lack and the source of truth for the fixtures Culprit already
tests against.

## Hosting posture (Mode B first, zero blast radius)

Mode B: Culprit self-hosts (Fly.io/Railway, ~$5/mo) and reaches their account with
the cross-account **read-only** role above (external-id-guarded `sts:AssumeRole`).
Zero footprint in their account beyond the role; the SNS subscription is an
outbound HTTPS POST to Culprit. Mode A (a second Fargate service in their cluster
via an `iac/` PR) is a later option once trust is established.

## The fixture → live upgrade path (no service code changes)

Everything Culprit runs on today is shape-faithful to the live shapes, so going
live is a **swap behind interfaces already tested**:

1. **Logs**: set AWS creds → `Boto3LogsProvider` replaces `FixtureLogsProvider`
   (same `LogsProvider` interface, `culprit/cloudwatch.py`). CloudWatch Logs
   Insights returns the same middleware exception JSON the fixtures contain.
2. **SNS**: stand up the alarm topic + subscribe `POST /ingest/sns` over HTTPS
   (the `SubscriptionConfirmation` handshake is implemented). Real deliveries carry
   the same envelope + `text/plain` + `x-amz-sns-*` headers the fixtures do, signed
   by **Amazon's** cert — verification switches from the vendored cert to fetching
   `SigningCertURL` under the existing `sns.<region>.amazonaws.com` allowlist
   (unset `SNS_SIGNING_CERT_PATH`; `SNS_SIGNATURE_STRICT` stays on).
3. **Metrics/impact**: the impact calculator's live source (`ALB RequestCount`/5xx)
   plugs into the same `compute_impact` interface.
4. **Re-run the eval** against real captures to refresh the numbers — the corpus
   fixtures are replaced in place (`fixtures/sns/PROVENANCE.md` documents the
   capture playbook), the service code is untouched.

## Privacy answers (proactive)

- Read-only role, no PII in briefs (Culprit cites SHAs, files, counts — not user
  data), offer-only remediation. Error data already flows to CloudWatch; Sentry +
  the LLM API are opt-in and can run in their own accounts.
