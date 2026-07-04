# Culprit — Executive Handoff

**Date:** 2026-07-03
**Status:** Research and design complete. Zero code written. This directory (`~/Culprit`) is the intended project root.
**Owner:** Ishan Ajwani ([ishan.ajw@gmail.com](mailto:ishan.ajw@gmail.com)) — building this as a flagship resume project.
**Prior context:** All decisions below were made in a prior Claude Code session after a 5-agent research workflow (4 researchers + 1 adversarial fact-checker; every load-bearing claim below was independently verified against primary sources on 2026-07-02).

---

## 1. What Culprit is

An AI incident-response service. When a production alert fires it: (1) identifies the likely culprit GitHub commit, (2) retrieves the right runbook — **offers it, never executes it**, (3) estimates user impact, (4) diagnoses the issue with cited evidence, (5) posts a brief to the team's chat, and (6) after resolution, drafts a postmortem as a GitHub PR.

**Pitch sentence (already user-approved):** "I'm building Culprit, a free AI incident-response layer for theCourseForum: it adds the monitoring you don't currently have, and when something breaks in production it automatically posts a brief to your team chat with the likely culprit commit, a diagnosis with evidence, estimated user impact, and the right runbook to fix it — then drafts the postmortem after you resolve it."

**Goal:** a robust, deployed tool in production at theCourseForum (UVA course-review platform, student-run nonprofit CIO), with honest eval numbers for the resume.

---



## 2. Partner intel: theCourseForum (all verified in their repo at commit b8d3fe1c, 2026-07-01)

Repo: `github.com/thecourseforum/theCourseForum2` (public, GPLv3). Default branch `dev`, deploys from `master`.

**Stack:** Python 3.12 + Django 4.2 (uv-managed, `pyproject.toml`, no requirements.txt), DRF 3.14, Postgres on RDS (db.t3.micro; postgres:17.5 locally), Redis ElastiCache (cache.t4g.micro) + django-cachalot ORM caching, Postgres TrigramSimilarity search (NO Elasticsearch), server-rendered Django templates + vanilla JS (no framework, no bundler), Cognito auth (email OTP, custom JWT backend), Gunicorn 3 workers × 2 threads on port 80.

**Infra:** AWS us-east-1, fully Terraform-managed in `iac/`: ECS Fargate (cluster `tcf-fargate-cluster`, service `barrett-fogle-love-v1`, default 1 task @ 0.5 vCPU/2GB), ALB, CloudFront, Route53, S3 static, ECR (`tcf/thecourseforum2`), Secrets Manager.

**CI/CD:** GitHub Actions. CI = ruff + djlint + eslint + `ty` typecheck + Django tests. CD (`aws.yml`) auto-deploys **every green master merge**: build → ECR → one-off ECS release task (`migrate && collectstatic && invalidate_cachalot && clearsessions`) → update task def → wait for stability. This matters: deploy windows are small (usually one PR), which makes culprit-commit ID unusually tractable here.

**Observability: NONE.** No Sentry/Datadog/anything (grep-verified). No CloudWatch alarms, dashboards, or SNS. Only: a `/health` middleware returning literal "ok" (ALB health checks), a middleware printing unhandled exceptions as JSON lines to stderr → CloudWatch (2 log groups, 7-day retention). No runbooks, no incident docs, no on-call. **Culprit therefore has nothing to trigger on today — instrumenting them is part of the project and the core of the pitch (we deliver their first monitoring stack).**

**Team/scale:** ~40-member student org, ~5–10 active committers (400 commits in past year, heavily concentrated). README claims "over 10k users each semester" — **the user's 20k+ figure is unverified; use 10k+ publicly.** They communicate through a discord server.

---



## 3. Feasibility verdicts (research-backed; don't re-litigate)


| Feature                        | Verdict                                                        | Key evidence                                                                                                                                                                                                                                                                                                                                                                                               |
| ------------------------------ | -------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Chat brief                     | Reliable                                                       | Trivial; every vendor ships it                                                                                                                                                                                                                                                                                                                                                                             |
| Runbook retrieval (offer only) | Reliable                                                       | Small-corpus retrieval; "never execute" matches universal industry practice — every GA vendor (Cleric, Rootly, PagerDuty, Sentry) is human-in-the-loop; practitioners explicitly distrust AI-executed runbooks (HN)                                                                                                                                                                                        |
| Postmortem drafting            | Reliable                                                       | Commodity GA feature: incident.io, Rootly, PagerDuty, FireHydrant all ship it                                                                                                                                                                                                                                                                                                                              |
| Impact estimation              | Doable if scoped                                               | Request-level counts near-exact (access logs, unsampled Sentry server errors); user-level counts are estimates industry-wide. Ship "~N failed requests, window W" exactly + hedged unique-user estimate with methodology stated                                                                                                                                                                            |
| Diagnosis                      | Useful, not guaranteed                                         | Ship ranked hypotheses w/ cited evidence + confidence; never a single asserted answer. ITBench (ICML 2025 oral): SOTA agents fully resolve only 13.8% of SRE scenarios                                                                                                                                                                                                                                     |
| Culprit commit                 | The hard one — reliable only under preconditions tCF satisfies | Meta: 42% top-5 accuracy (fine-tuned, monorepo, rich metadata). Sentry suspect commits = git-blame on stack frames, no published accuracy. Microsoft Teams study: only **27% of severe incidents are code bugs** (~40% incl. config); Google: ~40% of CI culprit searches have no culprit. **"No code culprit — looks infrastructural" must be a first-class output. Abstain below confidence threshold.** |


Key design consequences locked in:

- Culprit-commit analysis runs on **every** incident with a recent deploy, regardless of alert source. Alert source ≠ root-cause class (Sentry flood can be Redis dying; a silent hang can be a bad deploy).
- All code reads pinned to the **deployed SHA** (from deploy feed), never master HEAD.
- Deterministic code for anything countable (impact math, commit windows); LLM only phrases and reasons.
- Vendor accuracy claims (e.g., Sentry Seer "94.5%") are self-reported marketing — never benchmark against them; generate our own numbers via the eval harness.

---



## 4. Architecture (decided)

One containerized service + one Postgres. Everything else is traffic in/out.

```
        PUSH (webhooks in)                              PULL (evidence, read-only)
Sentry ─────────────┐                        ┌──→ GitHub (diffs, blame, files @ SHA)
SNS (CloudWatch) ───┼─→ FastAPI ingest ──────┼──→ CloudWatch Logs Insights + metrics
GitHub workflow_run ┘   creates SIGNALS      ├──→ Sentry API (issue detail)
                            │                └──→ Claude API (the loop)
                    correlation window (~10 min)
                    groups signals → INCIDENT
                            │
                    agent loop: gather → analyze → gather more →
                    rank suspects (or abstain) · pick runbook · compute impact
                            │
              Postgres (source of truth: incidents, signals, evidence, jobs, embeddings)
                            │
        ├─→ chat brief (living message; updates as signals join; resolve button)
        ├─→ on resolve: postmortem Markdown PR to their repo (humans merge)
        └─→ read-only web UI: full evidence trail per incident (demo artifact)
```

**Ingest specifics:**

- **Sentry (primary, build first, zero AWS dependency):** alert-rule webhook → `POST /ingest/sentry`. Payload has stack trace frames, release, users-affected — this is what powers culprit matching.
- **SNS (infra trigger):** CloudWatch alarm → SNS topic → HTTPS subscription → `POST /ingest/sns`. Thin payload. **Must handle the** `SubscriptionConfirmation` **handshake or nothing arrives.** Catches app-too-dead-for-Sentry failures (502s, crash loops, OOM).
- **GitHub** `workflow_run` **webhook →** `POST /ingest/github`**:** NOT a trigger — keeps the deploy timeline table current (SHA + timestamp per deploy). Polling the Actions API is an acceptable interim.
- **First qualifying signal starts the loop immediately** (speed is the product); later signals join the open incident, raise severity, update the brief. Dedup is critical — one outage fires multiple signals; multiple briefs per outage destroys credibility.

**Stack trace sources, in order:** Sentry webhook payload → Sentry API → CloudWatch Logs Insights (their middleware already dumps exception JSON to stderr, so traces are often in logs when Sentry is silent) → genuinely absent (pure infra; loop must still work).

**Resolution:** bot command/reaction (Discord) + auto-detect (alarm → OK, Sentry issue quiet post-deploy). Capture the fixing commit from the deploy feed — postmortem input.

**Postmortem:** assembled from timeline + culprit + measured impact + team's chat thread (read via chat API), opened as PR adding `postmortems/YYYY-MM-DD-slug.md` to theCourseForum2. Culprit drafts, humans merge. Never publishes unilaterally.

---



## 5. Tech stack (decided, mirrors their world deliberately)

- **Python 3.12 + FastAPI + uv + ruff + pytest + GitHub Actions** (matches their toolchain so their team can co-maintain).
- **Postgres + pgvector.** In-process async tasks + Postgres-backed job table (no Celery/SQS — incidents are rare; the job table doubles as a replay log).
- **Claude API:** Sonnet 5 workhorse (orchestration, diagnosis, culprit re-rank), Haiku 4.5 for cheap summarization. Cost at their volume: dollars/month.
- **Runbook retrieval starts dumb:** with 8–12 runbooks, put titles+summaries in the prompt and let the model pick — more reliable than embeddings at that corpus size. pgvector earns its keep later for "similar past incident" search. Build the interface, keep impl v1 simple.
- **Chat:** Discord webhook
- **GitHub App:** repo is public so code reads are free; App needed for webhooks, rate limits, and the ONE write permission (branch + PR for postmortems). Code access pattern: compare API for diffs, blame via GraphQL, files-on-demand at pinned SHA, ephemeral shallow clone only when repo-wide grep is needed. No persistent checkout.
- **AWS:** boto3 with read-only IAM. Exact ask for the VP: `logs:StartQuery/GetQueryResults/FilterLogEvents` (scoped to their 2 log groups), `cloudwatch:GetMetricData/DescribeAlarms`, `ecs:Describe*/ListTasks`, `elasticloadbalancing:Describe`*. Have the policy JSON ready to hand over.
- **Hosting, two modes, one image:** Mode B first (self-hosted Fly.io/Railway ~$5/mo, cross-account read-only creds, zero blast radius) → Mode A later (second Fargate service in their cluster via a Terraform PR in their `iac/` style).

---



## 6. Build roadmap

1. **Phase 1 — fault-injection harness FIRST, not the service.** Fork theCourseForum2, run locally (fully dockerized), wire Sentry free tier, write ~10 injectable faults (bad migration, N+1 timeout, template crash, Redis down, bad deploy). Record real webhook payloads as pytest fixtures. This harness is the demo (their incident rate is low), the eval source, and the origin of every resume number.
2. **Phase 2 — core pipeline MVP:** Sentry webhook → signal/incident model → evidence gathering → culprit ranking → chat brief. Demoable alone.
3. **Phase 3 — runbooks (author 8–12 for THEIR failure modes: DB conn exhaustion, ElastiCache down, migration rollback, Cognito outage — read their** `iac/` **for what can break), impact calculator, diagnosis synthesizer, SNS ingest.**
4. **Phase 4 — postmortem generator** (timeline + thread + fix commit → Markdown PR).
5. **Phase 5 — eval (top-1/top-3 culprit accuracy, retrieval precision, time-to-brief across N injected incidents), harden, then pitch.**

**Pitch meeting structure (decided):** open with "walk me through the last time the site broke," then a 2-min demo of Culprit diagnosing an injected fault on a fork of THEIR codebase, then the access asks. Bring two ready PRs: `sentry-sdk` in their pyproject + CloudWatch alarms/SNS in their Terraform.

---



## 7. Open questions (get answers from VP of Infra before hard-committing)

1. Last real incident — how found out, what broke, how fixed? (war story → demo scenario)
2. Who controls AWS; can they grant the read-only IAM role? (most likely stall point)
3. Will they merge the two instrumentation PRs? Any objection to error data → Sentry cloud + LLM API? (they hold student emails via Cognito)
4. Does `iac/` match deployed reality (Terraform drift, where's state)?
5. Staging env or prod+laptops only? (never inject faults into their prod — say so proactively)
6. How do semester data/grade loads run? (recent commit: "finish loading spring 26 grades" — incident source that isn't a deploy)
7. Rollback procedure today? (becomes runbook #1)
8. Deploy freezes / registration-week windows? Informal on-call, or "nobody at 11pm"? (if async, design brief for catch-up)
9. Who pays (target $0 their side), what happens at May graduation handoff, and what does agreed "success" look like (get a co-signable metric)?

---



## 8. Resume framing (user's core motivation — keep in view)

- Numbers come from the eval harness, e.g. "identified the culprit commit top-3 for X% of N injected production incidents; deployed for theCourseForum (10k+ users)." Interview-defensible only if the harness exists.
- Expect interviewers to probe the no-code-culprit case: knowing the 27%-of-incidents-are-code-bugs number and designing abstention is what separates this from a GPT-wrapper.
- "Offers the runbook, never runs it" is a deliberate, citable safety stance aligned with the entire industry — present it as a design decision, not a limitation.



## 9. Primary sources (for claims above)

- theCourseForum2 repo @ b8d3fe1c (stack/infra/CI facts; grep-verified observability absence)
- Meta eng blog 2024 "Leveraging AI for efficient incident response" — 42% top-5
- Microsoft Teams incident study, SoCC 2022 — 27% code bugs / mitigation distribution
- Google FACF paper (ICST 2023) — ~40% of culprit searches flake-caused; ~97.5% accuracy when culprit exists in range
- Rosa et al. ICSE 2021 — SZZ best F1 61%
- ITBench, arXiv:2502.05352 (ICML 2025 oral) — 13.8% SRE scenario resolution
- Sentry docs: suspect-commits mechanism/preconditions; spike protection & quota event-dropping; anonymous users keyed by IP
- Datadog Watchdog Impact Analysis docs — only shipped general user-impact estimator; requires RUM
- Google SRE Workbook — low-traffic services break %-based impact math; example postmortem states impact as estimated queries lost
- Vendor GA timeline: Sentry Seer GA 2025-06-17 ("94.5%" self-reported); Datadog Bits AI SRE GA 2025-12-02; PagerDuty SRE Agent GA 2025-10-31; incident.io AI SRE design-partners-only



## 10. Also know

- A project memory exists at `~/.claude/projects/-Users-ishanajwani-Documents-tindemo/memory/culprit-project.md` (written from the old session's project scope; this handoff supersedes it in detail).
- The prior session ran in `~/Documents/tindemo` (unrelated audio demo project); Culprit work belongs HERE in `~/Culprit`. Suggest `git init` + first commit of this file, then start with the fault-injection harness and the signal/incident data model.

