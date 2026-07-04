# Culprit — Implementation Plan

Culprit is an AI incident-response service for theCourseForum (UVA's course-review platform, ~10k+ users each semester). When a production alert fires, it identifies the likely culprit commit, retrieves the right runbook (offers it, never executes it), estimates user impact, diagnoses the issue with cited evidence, posts a living brief to the team's Discord, and — after resolution — drafts a postmortem as a GitHub PR. theCourseForum has no monitoring today, so Culprit also delivers their first observability stack. The end state: deployed in production for them, with honest eval numbers generated from a fault-injection harness.

## Ground rules

- **Read-only everywhere, with one exception.** All AWS, Sentry, and code access is read-only. The single write capability is the GitHub App's branch + pull-request permission on theCourseForum2, used only by the postmortem publisher. Culprit never merges, never pushes to protected branches, never executes a runbook, and never injects faults into their prod.
- **Deterministic code does everything countable** — impact math, commit windows, file-overlap and blame scores. The LLM ranks, reasons, phrases, and cites; it never does arithmetic it could cite instead.
- **Abstention is a first-class verdict.** Roughly a quarter to 40% of severe incidents aren't code bugs. Below a confidence threshold, the output is "no code culprit — looks infrastructural," not a guessed commit.
- **Suspects are ranked at commit granularity**, with the PR attached when one exists. theCourseForum auto-deploys every green push to master, and pushes are frequently direct commits with no PR — sometimes several deploys in one afternoon.
- **The deploy log distinguishes `attempted` from `promoted`.** Their release task can halt a deploy and the ECS circuit breaker can roll one back, so a green-looking workflow event alone never proves the live SHA changed. All code reads pin to the deployed SHA, never master HEAD.
- **PII discipline:** `send_default_pii=False` in every Sentry init; the database seed dump is scrubbed (and verified scrubbed) before touching the fork or any fixture; no real user data in this repo. They hold student emails — treat everything accordingly.
- Model IDs live in config and get pinned against current Anthropic docs at build time. Intent: Sonnet 5 as the workhorse (orchestration, diagnosis, re-ranking), Haiku 4.5 for cheap summarization.

## System design

One FastAPI container + one Postgres database (pgvector installed from day one; used later for similar-incident search). No Celery, no SQS — incidents are rare, so background work runs on in-process async workers pulling from a Postgres job table, which doubles as the durable replay log.

```
        PUSH (webhooks in)                          PULL (evidence, read-only)
Sentry ─────────────┐                       ┌──→ GitHub (diffs, blame, files @ SHA)
SNS (CloudWatch) ───┼─→ FastAPI ingest ─────┼──→ CloudWatch Logs Insights + metrics
GitHub workflow_run ┘   creates SIGNALS     ├──→ Sentry API (issue detail)
                            │               └──→ Claude API (agent loop)
                 correlation window (~10 min)
                 groups signals → one INCIDENT
                            │
                 agent loop: gather → analyze → gather more →
                 rank suspect commits (or abstain) · pick runbook · compute impact
                            │
          Postgres: signals, incidents, deploys, evidence, jobs,
                    agent transcripts, verdicts
                            │
        ├─→ living Discord brief (one message per incident, edited in place)
        ├─→ on resolve: postmortem Markdown PR to theCourseForum2
        └─→ read-only web UI: full evidence trail per incident
```

**Ingest.** Three endpoints, all creating signals — never incidents directly:
- `POST /ingest/sentry` — primary, richest payload (stack frames, release SHA, users affected). Zero AWS dependency; built first.
- `POST /ingest/sns` — CloudWatch alarms via SNS HTTPS subscription. Must handle the `SubscriptionConfirmation` handshake or nothing ever arrives. Catches app-too-dead-for-Sentry failures: 502s, crash loops, OOM.
- `POST /ingest/github` — `workflow_run` events keeping the deploy timeline current. Polling the Actions API is an acceptable interim.

**Correlation.** Signals dedup on (source, external id). The first qualifying signal starts the investigation immediately — speed is the product. Later signals within the ~10-minute window join the open incident, raise severity, and update the brief in place. One outage, one brief, always: multiple briefs per outage destroys credibility.

**Culprit analysis runs on every incident with a recent deploy, regardless of alert source** — a Sentry flood can be Redis dying, and a silent hang can be a bad deploy. Stack traces come from, in order: the Sentry webhook payload → the Sentry API → CloudWatch Logs Insights (their middleware already dumps exception JSON to stderr) → genuinely absent, in which case the loop still completes with an infra/abstain verdict.

**Chat is a Discord bot, not a bare webhook** — bare webhooks can't render buttons or read reactions, and both the resolve control and postmortem thread-reading need bot scopes.

**Code access pattern:** compare API for diffs, blame via GraphQL, files fetched on demand at the pinned SHA, ephemeral shallow clone only when repo-wide grep is needed. No persistent checkout.

---

## Phase 1 — Fault-injection harness (before the service)

The harness comes first because it is three things at once: the demo (their real incident rate is low), the eval source, and the origin of every number we'll ever claim. Everything it records becomes pytest fixtures for the service built in Phase 2.

**Work:**
1. Fork theCourseForum2. Fully dockerized local run: web + postgres:17 + redis (django-cachalot enabled under a prod-like settings shim) + toxiproxy. Create the `pg_trgm` extension. Stand up a stub JWKS server so login flows work without Cognito.
2. Scrub the seed dump before first use — `harness/scrub_dump.py` plus a verifier that fails loudly on residual emails or user-identifying data. If no dump is available, fall back to factory data (search quality degrades; most faults still work).
3. Wire Sentry free tier to the fork: `sentry-sdk[django]`, release stamped with the git SHA via Docker build arg, `send_default_pii=False`, alert rule → webhook. This same diff doubles as the ready-made upstream PR later.
4. Build ~10 injectable faults, each a fork branch with labeled ground truth:
   - *Culprit cases:* school-rename → landing-page KeyError; empty semester table → course-page 500s; bad migration (ground truth: "deploy never promoted," not a culprit); template crash; N+1 query → timeout; silent search loss (bulk write bypasses `save()`-maintained search columns — no exception fires; enters via a synthetic monitor signal).
   - *Abstention cases:* Redis down (sitewide 500s — cachalot has no fallback); Redis slow (worker exhaustion via toxiproxy); JWKS blackhole (login workers hang on an untimed `urlopen`); config-only change with zero commits in the window.
5. Fire each fault and record the real webhook payloads into `fixtures/`, labeled with ground truth.
6. Run the deploy-log backfill now: a script paging theCourseForum2's GitHub Actions deployment-run history into a dataset (a deploy counts as `promoted` only on a fully green run). Actions retention is finite — every week of delay loses history.
7. Verify the assumed fault sites against the fork before building on them: the hard-coded featured-schools dict in the browse view, the `Semester.latest()` dereference, the untimed JWKS `urlopen` in the auth backend.

**Done when:** all faults fire and produce recorded fixtures with ground-truth labels, the scrub verifier passes, and the backfill dataset exists.

## Phase 2 — Core pipeline MVP (demoable alone)

Sentry webhook → signal/incident model → evidence → culprit ranking → Discord brief.

**Work:**
1. Service scaffold: uv project, FastAPI, ruff + pytest + GitHub Actions CI, Dockerfile, docker-compose (app + postgres). First commit includes this plan.
2. Schema + Alembic migrations:
   - `signals` — source, external id, unique dedup key, raw payload JSONB, received_at, incident FK
   - `incidents` — status (`open → investigating → briefed → resolved | abstained`), severity, opened/closed timestamps
   - `deploys` — sha, previous_sha, status (`attempted | promoted`), workflow run id, commit/PR list, timestamps
   - `jobs` — kind, payload, status, attempts, `run_after`; claimed via `SELECT … FOR UPDATE SKIP LOCKED`; bounded retries
   - `agent_runs` — full transcript JSONB, token counts, outcome
   - `verdicts` — kind (`culprit | abstain | not_promoted`), ranked suspects JSONB with evidence citations, confidence, summary
3. `POST /ingest/sentry`: shared-secret auth, tolerant parsing, always respond fast; malformed payloads stored raw, never 500'd. Correlator joins-or-creates and enqueues the investigation on the first signal immediately; later signals raise severity and trigger a brief edit. Duplicate delivery is a no-op.
4. Deploy timeline: seed from the Phase 1 backfill; refresh by polling the Actions API on a schedule. Window computation: incident time → last *promoted* deploy → suspect commits between `previous_sha` and `sha`, PRs attached where they exist, plus a `latest_attempt_not_promoted` flag that feeds the bad-migration verdict.
5. Evidence gathering with injected clients so tests replay from fixtures: GitHub diffs/blame/files at the pinned SHA; Sentry API enrichment; CloudWatch gatherers stubbed until the IAM role exists.
6. Ranking: deterministic feature scoring per suspect commit (file overlap with stack frames, blame hits, deploy adjacency, churn size) → Claude tool-use loop re-ranks and diagnoses with citations, or abstains; every transcript persisted. LLM calls run through a record/replay cassette layer so CI is deterministic and free; live mode behind a flag.
7. Discord bot posts the living brief: severity, deploy window, top suspects with confidence and cited evidence — diagnosis always as ranked hypotheses, never a single asserted answer — or the abstention / deploy-never-promoted rendering. Edits in place as the incident evolves. A console/markdown notifier ships alongside for CI, evals, and demos without Discord.

**Done when:** an injected fault on the fork produces exactly one living brief naming the right commit (or correctly abstaining) with cited evidence, and the entire run replays from fixtures in pytest with no network.

## Phase 3 — Runbooks, impact, diagnosis synthesis, SNS

**Work:**
1. Author 8–12 runbooks for their actual failure modes, derived from reading their infrastructure: DB connection exhaustion, ElastiCache down, migration rollback, Cognito outage, disk/OOM, semester grade-load job failures. Retrieval starts dumb — titles + summaries in the prompt, model picks; the interface leaves room for pgvector similar-incident search later. Runbooks are offered in the brief, never executed.
2. Impact calculator (fully deterministic): "~N failed requests over window W" computed exactly from unsampled Sentry server-error counts and, once log access exists, ALB/access-log queries. Unique-user figures are always estimates, stated with methodology.
3. Diagnosis synthesizer: merges ranker output, log evidence, and impact into the brief's hypothesis section.
4. `POST /ingest/sns` with the `SubscriptionConfirmation` handshake. The CloudWatch alarms that feed it ship via a Terraform PR to their repo — but only after confirming which infrastructure actually serves production, since their `iac/` module as written provisions a test-domain stack with no state backend and currently defines zero alarms.

## Phase 4 — Postmortem generator + resolution

**Work:**
1. Resolution: Discord bot command or reaction, plus auto-detection (CloudWatch alarm back to OK; Sentry issue quiet after a subsequent deploy). The fixing commit is captured from the deploy feed.
2. Postmortem draft assembled from the incident timeline, culprit verdict, measured impact, and the team's Discord thread (read via bot scope); Haiku summarizes the thread.
3. Publisher opens a PR adding `postmortems/YYYY-MM-DD-slug.md` to theCourseForum2 via the GitHub App's branch + PR permission — the one write in the whole system. Humans edit and merge; Culprit never merges.

## Phase 5 — Eval, web UI, hardening, pitch

**Work:**
1. Eval harness across all Phase 1 faults, N trials each, one command to regenerate the table:
   - top-1 / top-3 culprit accuracy **versus the naive baseline "blame the latest deploy"** — without the baseline the number is hollow, and because their deploy windows are often a single commit, the baseline is strong; the differentiation lives in abstention cases and multi-commit windows, so keep both heavily represented
   - abstention precision/recall
   - runbook-retrieval precision
   - median time-to-brief
2. Read-only web UI: per-incident evidence trail (signals, timeline, evidence with citations, verdict, full transcript). Server-rendered, zero writes. This is the pitch-demo artifact.
3. Hardening: rate limits, payload-size caps, idempotent redelivery, secret hygiene, graceful degradation when any evidence source is dark.
4. Pitch meeting: open with "walk me through the last time the site broke," run the 2-minute demo of Culprit diagnosing an injected fault on the fork of *their* codebase, then make the access asks with the read-only IAM policy JSON ready to hand over (`logs:StartQuery/GetQueryResults/FilterLogEvents` on their two log groups; `cloudwatch:GetMetricData/DescribeAlarms`; `ecs:Describe*/ListTasks`; `elasticloadbalancing:Describe*`). Important sequencing: their repo has stalled `sentry` and `structured-logging` branches someone already wrote — talk to that author first and offer them the landing, rather than showing up with a competing PR.
5. Deploy Mode B: this container on Fly.io/Railway (~$5/month) with a managed Postgres and cross-account read-only creds. Mode A — a second Fargate service inside their cluster via a Terraform PR in their style — comes later; same image either way.

---

## Open questions for theCourseForum (get answers before hard-committing)

1. Last real incident — how did they find out, what broke, how was it fixed?
2. Who controls AWS, and can they grant the read-only IAM role?
3. Will they merge the instrumentation PRs? Any objection to error data flowing to Sentry cloud and an LLM API, given they hold student emails?
4. Does `iac/` match deployed reality? Where is Terraform state? (Gates the alarms PR.)
5. Staging environment, or prod + laptops only?
6. How do semester data/grade loads run? (An incident source that isn't a deploy.)
7. What's the rollback procedure today? (Becomes runbook #1.)
8. Deploy freezes during registration week? Any informal on-call, or nobody at 11pm?
9. Who pays (target: $0 on their side), what happens at May graduation handoff, and what does co-signable success look like?
10. Who wrote the `sentry` and `structured-logging` branches, and do they want to land that work themselves?

## Risks

- **No deadline gate is set.** Phase 2's "demoable alone" milestone is the natural checkpoint — set a date for it, and if the date arrives mid-phase, ship what exists rather than polishing.
- **Baseline strength.** Single-commit deploy windows mean "blame the latest deploy" is nearly as accurate as the tool on easy culprit cases. The eval must lean on abstention scenarios and multi-commit windows or the headline number won't differentiate.
- **Politics.** The IAM grant is the likeliest stall point; the stalled-branch author is the likeliest turf conflict; the alarms PR is blocked until the infrastructure question is answered. None of this blocks Phases 1–2, which need nothing from them.
- **Sentry quota:** free tier drops events under spike protection — the harness should confirm briefs still form from CloudWatch/log evidence when Sentry samples or drops.
