# Culprit — AI Incident-Response Layer for theCourseForum

## Problem
theCourseForum (UVA course-review platform, 10k+ users/semester, ~5–10 active student committers) has **zero observability**: no Sentry, no CloudWatch alarms, no runbooks, no on-call (grep-verified at repo commit b8d3fe1c, 2026-07-01). When production breaks, the team finds out from users, diagnoses ad-hoc, and writes no postmortems. Separately, the project owner (Ishan) needs a flagship resume project with honest, interview-defensible eval numbers — commodity CRUD apps don't differentiate.

## Evidence
- Grep-verified absence of any monitoring in `github.com/thecourseforum/theCourseForum2`: only a `/health` middleware returning "ok" and a middleware printing unhandled exceptions as JSON to stderr → CloudWatch (7-day retention).
- 5-agent research workflow (4 researchers + 1 adversarial fact-checker, 2026-07-02) verified every load-bearing feasibility claim against primary sources (Meta eng blog, Microsoft SoCC 2022, Google ICST 2023, ITBench/ICML 2025, Sentry/Datadog docs).
- tCF's CD auto-deploys every green master merge, so deploy windows are usually one PR — this makes culprit-commit identification unusually tractable for this partner (verified in their `aws.yml` workflow).
- Pitch sentence already user-approved; feasibility verdicts per feature locked in HANDOFF.md §3 — do not re-litigate.

## Users
- **Primary**: theCourseForum maintainers (student org, VP of Infra is the gatekeeper). Trigger: a production alert fires — they need to know what broke, which commit likely caused it, how many users are affected, and what to do about it, delivered to their Discord.
- **Secondary**: Ishan, who needs the deployed system plus an eval harness producing resume-grade numbers.
- **Not for**: multi-tenant SaaS customers, arbitrary orgs, or any team expecting the AI to *execute* remediation (it never will, by design).

## Hypothesis
We believe **an AI incident-response service that ingests alerts (Sentry, SNS, GitHub deploys), ranks likely culprit commits (or explicitly abstains), offers — never executes — the right runbook, estimates impact deterministically, posts a living brief to Discord, and drafts postmortem PRs** will **give theCourseForum its first real incident-response capability at $0 cost** and **produce interview-defensible eval numbers**.
We'll know we're right when **the eval harness reports top-1/top-3 culprit accuracy across N injected incidents, time-to-brief is measured in minutes, and Culprit runs in production for theCourseForum**.

## Success Metrics
| Metric | Target | How measured |
|---|---|---|
| Culprit-commit top-3 accuracy | Honest measured number (industry SOTA context: Meta 42% top-5) across ≥10 injected incident scenarios | Fault-injection eval harness (Phase 1/5) |
| Abstention correctness | "No code culprit — looks infrastructural" emitted on non-code faults (Redis down, infra) | Eval harness, labeled scenarios |
| Time-to-brief | First Discord brief within minutes of first qualifying signal | Timestamps in eval harness runs |
| Dedup | Exactly 1 brief per outage regardless of signal count | Eval harness multi-signal scenarios |
| Runbook retrieval precision | Correct runbook offered for its failure mode | Eval harness, labeled scenarios |
| Deployed in production | Running for theCourseForum with their sign-off | Pitch meeting → access granted → live |
| Cost to tCF | $0 their side; LLM cost dollars/month | Billing |

## Scope
**MVP** — Fault-injection harness on a fork of theCourseForum2 (≈10 injectable faults, recorded webhook payloads as pytest fixtures) + core pipeline: Sentry webhook → signal/incident model with correlation-window dedup → evidence gathering pinned to deployed SHA → culprit ranking with abstention → Discord brief. This alone is demoable and tests the hypothesis's hardest claim (culprit ID).

**Out of scope**
- Executing runbooks or any remediation — deliberate, citable safety stance; offer-only, permanently.
- Exact unique-user impact counts — industry-wide these are estimates; ship exact request counts + hedged user estimate with stated methodology.
- Single asserted root cause — diagnosis is always ranked hypotheses with cited evidence and confidence, or abstention.
- Elasticsearch/heavy vector infra at v1 — 8–12 runbooks fit in a prompt; pgvector reserved for later similar-incident search.
- Celery/SQS — incidents are rare; Postgres job table suffices and doubles as replay log.
- Unilateral postmortem publishing — Markdown PR only; humans merge.
- Benchmarking against vendor self-reported accuracy claims (e.g., Sentry Seer "94.5%") — generate our own numbers only.
- Injecting faults into tCF production — harness runs on a fork, ever.

## Delivery Milestones
| # | Milestone | Outcome | Status | Plan |
|---|---|---|---|---|
| 1 | Fault-injection harness | Forked tCF runs locally dockerized with Sentry wired; ~10 reproducible faults; real webhook payloads captured as pytest fixtures — the demo, eval source, and origin of every resume number | in-progress | `.claude/plans/culprit-m1-fault-injection-harness.plan.md` |
| 2 | Core pipeline MVP | An injected fault produces a deduped Discord brief with ranked culprit commits (or abstention) within minutes, end-to-end demoable | pending | — |
| 3 | Full diagnosis layer | 8–12 runbooks authored for tCF's actual failure modes and offered correctly; deterministic impact numbers in briefs; SNS/CloudWatch ingest catches app-too-dead-for-Sentry failures | pending | — |
| 4 | Postmortem generator | Resolving an incident yields a postmortem Markdown PR (timeline + culprit + impact + chat thread) to their repo | pending | — |
| 5 | Eval, harden, pitch | Measured top-1/top-3 accuracy, retrieval precision, time-to-brief across N incidents; two instrumentation PRs ready (sentry-sdk + Terraform alarms); pitch delivered; deployed Mode B | pending | — |

## Open Questions
(From HANDOFF.md §7 — get answers from tCF's VP of Infra before hard-committing; none block Milestones 1–2, which run entirely on a fork.)
- [ ] Last real incident: how discovered, what broke, how fixed? (war story → demo scenario)
- [ ] Who controls AWS; can they grant the read-only IAM role? (most likely stall point)
- [ ] Will they merge the two instrumentation PRs? Objections to error data → Sentry cloud + LLM API? (they hold student emails via Cognito)
- [ ] Does `iac/` match deployed reality (Terraform drift, state location)?
- [ ] Staging env, or prod + laptops only?
- [ ] How do semester data/grade loads run? (incident source that isn't a deploy)
- [ ] Rollback procedure today? (becomes runbook #1)
- [ ] Deploy freezes / registration-week windows? Informal on-call or "nobody at 11pm"?
- [ ] Who pays (target $0 their side); May graduation handoff; co-signable success metric?

## Risks
| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| AWS read-only access stalls at VP/org level | Medium-high | Blocks SNS ingest + CloudWatch evidence (Milestone 3+) | Sentry-first build order has zero AWS dependency; exact scoped IAM policy JSON ready to hand over; Mode B hosting = zero blast radius |
| Real incident rate too low to demo/eval | High (certain) | No live proof | Fault-injection harness IS the demo and eval source — built first by design |
| Culprit commit is genuinely hard (27% of severe incidents are code bugs; ~40% of culprit searches find none) | High | Wrong answers destroy credibility | Abstention as first-class output; ranked hypotheses with cited evidence, never a single asserted answer; run analysis only when a recent deploy exists |
| Multiple briefs per outage destroy trust | Medium | Team ignores the bot | Correlation window (~10 min) dedup; later signals join open incident and update the living brief |
| Sentry free-tier quota drops events during spikes | Medium | Missing evidence during the worst incidents | CloudWatch logs fallback (their middleware already dumps exception JSON to stderr); SNS path independent of Sentry |
| tCF declines (privacy: student emails via Cognito; error data to Sentry cloud + LLM) | Medium | No production deployment | Proactive answers in pitch: read-only IAM, no PII in briefs, offer-only stance; fork-based demo works regardless |
| May graduation handoff — no maintainer continuity | Medium | Project dies post-deploy | Stack mirrors theirs (Python/uv/ruff/GH Actions) so their team can co-maintain; raised explicitly in pitch Q9 |

---
*Status: DRAFT — requirements only. Implementation planning pending via /plan.*
*Source: HANDOFF.md (2026-07-03), committed at repo root. Research verified 2026-07-02.*
