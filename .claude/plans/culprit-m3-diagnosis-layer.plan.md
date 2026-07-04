# Plan: Culprit Milestone 3 — Full Diagnosis Layer

**Source PRD**: `.claude/prds/culprit.prd.md`
**Selected Milestone**: 3 — Full diagnosis layer
**Complexity**: Large

## Summary
Extend the M2 service (`culprit/`) into the full diagnosis layer: **SNS/CloudWatch ingest** (`POST /ingest/sns` with the `SubscriptionConfirmation` handshake) pulls the 9 silent-fault runs into the eval (top-k N grows 10 → 18, abstention 2 → 3); **runbook retrieval** offers one of 8–12 runbooks authored for tCF's actual failure modes (their `iac/` is the source: RDS, ElastiCache, ECS Fargate, ALB, Cognito, migrations); a **deterministic impact calculator** puts exact request counts + a hedged, methodology-stated user estimate in every brief; and a **diagnosis synthesizer** renders ranked hypotheses with cited evidence and confidence — never a single asserted answer. pgvector enters for "similar past incident" search.

**The gating risk is AWS read-only access (HANDOFF §7 Q2 — likely stall point), so the milestone is sequenced to need ZERO live AWS.** Tasks 1–5 (runbooks, impact, diagnosis, pgvector) touch no AWS shape at all; Tasks 6–9 build the SNS/CloudWatch path against **shape-faithful synthesized fixtures** (mirroring exactly how M1 backfilled `workflow_run` in `harness/deployfeed.py`: real field schema, real substantive objects, only opaque ids/timestamps synthesized, provenance documented); Task 10 delivers the exact scoped IAM policy JSON and the CloudWatch-alarms Terraform proposal so the moment the VP says yes, live AWS swaps in behind interfaces already tested.

## Patterns to Mirror
M1/M2 established Culprit's conventions; M3 extends them. tCF refs are for infra shape only.

| Category | Source (`file:line`) | Pattern |
|---|---|---|
| Synthesized-fixture provenance | `harness/deployfeed.py:11-47`, `fixtures/github/workflow_run/PROVENANCE.md` | Real field schema from a vendored real object; real substantive fields; only opaque ids/timestamps synthesized (deterministically, byte-stable); `"reconstructed": true` stamp; provenance doc; key-parity test (`tests/test_deployfeed.py`) |
| Fixture envelope | `harness/deployfeed.py:252-293` | Recorder envelope (`received_at`, `source`, `resource`, `headers`, `raw_body` latin-1) IS the ingest contract — SNS fixtures use the same wrapper |
| Ingest boundary | `culprit/ingest/sentry.py`, `culprit/app.py` | verify-then-parse, idempotent on `dedup_key`; 401 on bad/missing signature; raw payload preserved in jsonb |
| Signature verify | `culprit/signatures.py` | Constant-time verification over the raw body; SNS adds X.509 (`SigningCertURL`) — same "never trust an unsigned request" stance |
| Env-gated integrations | `culprit/config.py:26-51`, `docs/pipeline.md` (Tests §) | Every secret optional; absent secret → integration inert + its tests skip; deterministic pipeline runs with none |
| Deterministic-decides / LLM-phrases | `culprit/pipeline.py:150-158`, `culprit/llm.py` | LLM gated behind `enabled`, only phrases; deterministic scores are authoritative; eval never scores LLM output |
| Ranking composite | `culprit/ranking.py` (HANDOFF-M2 §5.1) | The file/stem/symbol/blame composite is load-bearing — extend for frameless evidence, never simplify it away |
| Eval anti-leakage | `culprit/eval/replay.py:1-9`, `culprit/eval/score.py:1-7` | Pipeline sees only ingest contract + deploy feed; the scorer is the ONLY ground-truth reader |
| Honest per-class N | `culprit/eval/score.py:53-89` | Per-class denominators; deferred ≠ missed; publish N with every rate |
| Self-describing models | `culprit/models.py:1-7` | Flat, docstring-stated contracts; jsonb `raw`/`payload` preserves full inbound data |
| Deps + CI | `pyproject.toml`, `.github/workflows/ci.yml` | uv/py3.12/ruff/pytest; bounded ranges; CI postgres service + migrate step |
| tCF iac style | fork `iac/*.tf` (`cloudwatch.tf` = 2 log groups, zero alarms today) | The alarms proposal mirrors their Terraform naming/layout (`local.name_prefix`, tagged resources) |

## Architecture decisions (this milestone)

1. **M3 extends `culprit/`; `harness/` gains only the SNS backfill generator.** M2's promise holds: `signals`/`incidents` accept the new source with **no schema change** (`source="cloudwatch"`, `kind="alarm"`, `dedup_key=MessageId`, `fingerprint=AlarmName`, `frames=[]`, `release=NULL` — all fit `culprit/models.py:72-100` as-is). New columns are additive features only: `incidents.diagnosis` (jsonb) and `incidents.embedding` (pgvector). Extending `harness/` mirrors the M1 precedent — `deployfeed.py` was itself a post-hoc backfill.
2. **Sequencing quarantines the AWS risk.** Tasks 1–5 = zero AWS in any form (runbooks, retrieval, impact v1, diagnosis, pgvector — all build on the existing evidence/jobs audit trail and `issue.count`). Tasks 6–9 = the SNS/CloudWatch path against synthesized fixtures + a fixture-backed logs provider (still zero live AWS). Task 10 = the handover pack (IAM policy JSON, alarms proposal, live-capture upgrade path). Nothing in M3 blocks on the VP of Infra.
3. **The alarm suite is designed NOW and is the fixtures' source of truth.** tCF has zero alarms today (`iac/cloudwatch.tf` = two log groups). `docs/aws/alarms-proposal.tf` (their iac style — also the second pitch PR per HANDOFF §6) defines: ALB `TargetResponseTime` (latency), ALB `HTTPCode_ELB_5XX_Count`/`HTTPCode_Target_5XX_Count`, ALB `UnHealthyHostCount`, ECS `MemoryUtilization`, RDS `DatabaseConnections`/`CPUUtilization`, ElastiCache health, and a **search-smoke synthetic canary** (known-good query must return >0 results). The canary is stated honestly: it is the *only* plausible detector for `search-silent-zero-results` — without it that fault class is invisible in production (that's part of the pitch for it).
4. **Synthesized SNS fixtures, deployfeed-grade provenance.** `harness/snsfeed.py` (`culprit-harness backfill-sns`) generates one fixture per silent run + the two infra dedup runs (11 total): the *schema* is the real SNS HTTPS-delivery envelope (`Type`, `MessageId`, `TopicArn`, `Subject`, `Message`, `Timestamp`, `SignatureVersion`, `Signature`, `SigningCertURL`, `UnsubscribeURL`; headers `x-amz-sns-message-type`/`-message-id`/`-topic-arn`; **`Content-Type: text/plain; charset=UTF-8`** — the classic gotcha) vendored from AWS's published message formats; the `Message` body is the real CloudWatch alarm state-change JSON whose alarm names/metrics/dimensions come from the alarms proposal (decision 3) and whose timestamps derive from the run's `injected_at` + a documented alarm-evaluation delay; only opaque ids are synthesized (deterministically, byte-stable). Stamped `"reconstructed": true`; `fixtures/sns/PROVENANCE.md` documents every synthesized field and the **path to a real capture later** (stand up topic+alarm in a personal AWS account or theirs post-grant, subscribe the recorder over HTTPS, re-record — the M1 deploy-webhook playbook).
5. **SNS signatures verify for real, against a vendored keypair.** `culprit/sns_verify.py` implements genuine SNS verification: canonical string per `SignatureVersion` 1/2, X.509 cert fetched from `SigningCertURL` with a **strict host allowlist** (`sns.<region>.amazonaws.com`, https only — SSRF guard), signature check via `cryptography`. Fixtures are signed with a locally generated keypair whose cert is vendored (`harness/snsfeed_inputs/`), so the *real verification code path* runs offline — the M1 spirit ("genuine secret over reconstructed content"), with the delta documented: the cert is ours, not Amazon's; live mode pins the amazonaws.com allowlist.
6. **`POST /ingest/sns` = handshake + verify-then-parse, idempotent.** `SubscriptionConfirmation` → validate signature + `SubscribeURL` host allowlist → GET it via httpx (confirms the subscription; recorded as a `jobs` row for audit, not a Signal). `Notification` → parse `Message` → `Signal` (decision 1 mapping). `UnsubscribeConfirmation` → acknowledged, no Signal. Duplicate `MessageId` → no second row (unique `dedup_key`).
7. **Cross-source dedup: an alarm joins the open incident.** An SNS signal within `CORRELATION_WINDOW_SECONDS` of an open incident joins it (severity may rise) rather than opening a second one; otherwise it opens an incident and starts the loop (first qualifying signal, HANDOFF §4). Fingerprints differ across sources by nature (Sentry title vs AlarmName), so the join is windowed-time + single-service scope — a documented tradeoff (tCF runs one service; concurrent distinct outages are rare; revisit if false-merges appear). The eval gets a two-source case: `redis-down` replayed with its Sentry fixtures **and** its SNS fixture must yield exactly **1 incident/brief** (the PRD dedup metric, now cross-source).
8. **Frameless evidence: CloudWatch Logs Insights behind a provider interface.** `culprit/cloudwatch.py` defines `LogsProvider`: `Boto3LogsProvider` (live; `boto3` via `asyncio.to_thread`; env-gated on AWS creds) and `FixtureLogsProvider` (reads `fixtures/logs/*.log` — the 22 captures that already exist). Stack-trace source order per HANDOFF §4: webhook frames → Sentry API → **logs** → genuinely absent (loop still works). `culprit/logparse.py` parses their middleware's stderr exception JSON (`{"level":"ERROR","path":...,"ip":...,"exception":...,"traceback":...}`) into the existing frames shape, and gunicorn markers (`WORKER TIMEOUT`, boot/SIGKILL churn) into infra-class markers.
9. **Ranking extends; the M2 composite is untouched for frame-ful incidents** (HANDOFF-M2 gotcha 1 — do not simplify it). New deterministic signals apply only when webhook frames are absent: (a) log-derived frames feed the *existing* composite unchanged; (b) with no frames anywhere, an alarm-class → diff-surface affinity scores the window (latency alarm → commits removing `select_related`/`prefetch_related`, adding joins/`annotate`, or migrations dropping indexes; canary/search alarm → commits touching the search module; memory/5xx alarms with no code affinity → abstain) — with a **higher abstention bar** than the frame path. Deterministic decides; silent-fault accuracy is whatever it honestly is.
10. **Impact is deterministic with the methodology stated on every number.** `culprit/impact.py` computes: exact failed-request count (source order: Sentry `issue.count` → logs-provider error-line count in the incident window → live ALB `RequestCount`/5xx metrics) and a hedged unique-user estimate (Sentry `userCount`, IP-keyed per Sentry docs → distinct client IPs in the logs), each tagged with its method string. The brief renders "~N failed requests over W; est. ~U users (method: …)" (HANDOFF §3: request counts near-exact, user counts are estimates industry-wide). The LLM never computes a number.
11. **Runbook retrieval v1 = titles+summaries in the prompt, model picks (HANDOFF §5).** `runbooks/*.md` with structured frontmatter (`id`, `title`, `summary`, `failure_mode`, `symptoms`, `checks`, `steps`, `rollback`) — **offer-only, never execute** (permanent stance). `culprit/runbooks.py` loads/validates the corpus and defines the `RunbookSelector` interface; v1 impl sends titles+summaries to Sonnet (temperature 0, output constrained to corpus ids), env-gated on `ANTHROPIC_API_KEY` — absent key → no runbook section (integration-inert convention). pgvector is *not* used for runbook selection (8–12 docs fit in a prompt); it enters for similar-incident search only.
12. **Runbook precision is scored honestly, in a gated eval section.** `culprit/eval/runbook_labels.yaml` maps `fault_id` → expected runbook id and is read **only by the scorer** (anti-leakage: the pipeline never sees `fault_id`; the selector sees only incident context). Because selection is LLM-driven, this eval section runs only with the key present (temp 0 for stability), is reported with its own N, and never pollutes the deterministic culprit/abstention numbers.
13. **pgvector for "similar past incident".** Alembic migration: `CREATE EXTENSION vector` + `incidents.embedding`; docker-compose + CI move to the `pgvector/pgvector:pg17` image (drop-in for postgres:17). Embeddings via the Voyage REST API (Anthropic's recommended embeddings provider) through httpx — no new SDK dep; `VOYAGE_API_KEY` env-gated, absent → inert. On incident open: embed title+frames text, nearest-neighbor over resolved incidents, cite matches in the brief ("similar to incident #N"). Eval: the corpus's w1/w4 siblings are natural labeled pairs — does a fault's second run retrieve its first? Own N, gated.
14. **Diagnosis = ranked hypotheses with citations, never one answer.** `culprit/diagnosis.py` deterministically assembles hypotheses: code-culprit (from ranking, citing its diff/blame `evidence` row ids), infra classes (from error class / alarm class — e.g. `ConnectionError` flood → cache/db outage), and always at least one alternative (or an explicit "insufficient evidence" floor). Confidence bands (high/medium/low) map from deterministic score ratios with fixed thresholds. Persisted as `incidents.diagnosis` jsonb (hypotheses + selected runbook + impact snapshot — the M4 postmortem input); Sonnet phrases the narrative only. The brief renders the top hypotheses with confidence + evidence citations.
15. **Anti-leakage is re-asserted at every new boundary.** The SNS generator never leaks fault identity into payloads (alarm names/TopicArn are fault-agnostic — the `head_branch: master` precedent); the logs the pipeline reads are the same stderr captures a real CloudWatch tail would contain; corpus tests extend to SNS fixtures (no orphans, signatures verify, non-leaking). Eval classes stay separately denominated: Sentry-visible top-k (N=10) is **not** silently averaged with SNS-silent top-k (N=8) — both are reported per-class *and* combined.

## Corpus & eval deltas (the N math)

All 22 recorded runs enter the eval. Fixture assignment (Task 6) and expected classes (Task 9):

| Class | M2 N | M3 N | Trigger in replay |
|---|---|---|---|
| Culprit top-k — Sentry-visible code faults | 10 | 10 | Sentry fixtures (unchanged) |
| Culprit top-k — silent code faults | deferred | **8** | synthesized SNS alarm + `FixtureLogsProvider` |
| Abstention (infra) | 2 | **3** | `gunicorn-oom` joins via SNS (no Sentry event by design) |
| Baseline (benign deploy → no incident) | 1 | 1 | nothing fires (no alarm fixture) |
| Cross-source dedup (Sentry + SNS → 1 incident) | — | 1 case | `redis-down` w3 with both fixture sets |
| Runbook precision (gated, LLM) | — | 21 | every incident-producing run |
| Similar-incident retrieval (gated, pgvector) | — | ~9 pairs | w1/w4 siblings |

Fault → alarm mapping (drives `snsfeed.py`; alarm definitions live in `docs/aws/alarms-proposal.tf`):

| Run(s) | Alarm fixture | CloudWatch source |
|---|---|---|
| `n-plus-one-section-instructor-prefetch` w1/w4 | `alb-target-response-time` | ALB `TargetResponseTime` p95 |
| `cartesian-join-gpa-annotation-timeout` w1/w4 | `alb-target-response-time` | worker-timeout 502/504s → latency + 5xx |
| `bad-migration-drop-trigram-gin-indexes` w1/w4 | `alb-target-response-time` | seq-scan search latency |
| `search-silent-zero-results` w1/w4 | `search-canary` | Synthetics canary: known query returns 0 |
| `gunicorn-worker-oom` w3 | `alb-5xx` | ELB 502s from SIGKILLed workers |
| `redis-down` w3 (dedup case) | `elasticache-health` | joins the Sentry-opened incident |
| `db-stopped` w3 (dedup case) | `rds-connections` | joins the Sentry-opened incident |

Runbook catalog (Task 1; 10 core + 2 optional = 8–12, grounded in fork `iac/`):
`rollback-bad-deploy` (ECS task-def/image revert — HANDOFF §7 Q7 says this becomes runbook #1), `bad-migration-rollback` (migrate-applied-but-deploy-halted skew), `rds-outage-conn-exhaustion` (db.t3.micro limits), `redis-elasticache-down` (cachalot wraps every ORM read, no `IGNORE_EXCEPTIONS`), `ecs-oom-crashloop` (0.5 vCPU/2GB task), `alb-5xx-triage`, `perf-latency-regression` (N+1 / cartesian joins / lost indexes — EXPLAIN + prefetch checks), `search-zero-results`, `cognito-auth-outage` (email OTP/JWT backend), `app-error-spike-after-deploy` (Sentry flood → culprit workflow); optional: `cloudfront-s3-static-assets`, `secrets-manager-env-drift`.

## Files to Change

| File | Action | Why |
|---|---|---|
| `pyproject.toml` | UPDATE | Add `boto3`, `pgvector`, `cryptography` (bounded ranges) |
| `docker-compose.yml`, `.github/workflows/ci.yml` | UPDATE | Postgres image → `pgvector/pgvector:pg17` (decision 13) |
| `culprit/config.py` | UPDATE | `aws_region`, `cloudwatch_log_groups`, `sns_allowed_topic_arns`, `sns_signature_strict`, `voyage_api_key`, `runbooks_dir` — all optional/inert-by-default |
| `culprit/models.py` + `migrations/versions/<new>` | UPDATE/CREATE | `incidents.diagnosis` (jsonb), `incidents.embedding` (vector); pgvector extension; **no `signals` change** |
| `runbooks/*.md` (8–12) | CREATE | The authored corpus (decision 11 frontmatter; offer-only) |
| `culprit/runbooks.py` | CREATE | Corpus loader/validator + `RunbookSelector` + prompt-pick v1 |
| `culprit/impact.py` | CREATE | Deterministic exact counts + hedged users, method strings (decision 10) |
| `culprit/diagnosis.py` | CREATE | Hypothesis assembly + confidence + evidence citations (decision 14) |
| `culprit/similar.py` | CREATE | Voyage embeddings (httpx) + pgvector nearest-neighbor (decision 13) |
| `culprit/sns_verify.py` | CREATE | SNS X.509 signature verify + host allowlists (decision 5) |
| `culprit/ingest/sns.py` | CREATE | Handshake + Notification parse → Signal (decision 6) |
| `culprit/app.py` | UPDATE | `POST /ingest/sns` |
| `culprit/correlation.py` | UPDATE | Cross-source windowed join (decision 7) |
| `culprit/cloudwatch.py` | CREATE | `LogsProvider` protocol: Boto3 (gated) + Fixture impls (decision 8) |
| `culprit/logparse.py` | CREATE | Middleware exception-JSON → frames; gunicorn infra markers |
| `culprit/ranking.py` | UPDATE | Frameless path: log-frames → existing composite; alarm-class diff-surface affinity + higher abstention bar (decision 9) |
| `culprit/pipeline.py` | UPDATE | Wire logs fallback, impact, runbook, diagnosis, similar-incident |
| `culprit/brief.py` | UPDATE | Impact / Suggested runbook (offer-only) / Diagnosis / Similar-incident sections |
| `culprit/llm.py` | UPDATE | Runbook pick (constrained ids, temp 0) + diagnosis phrasing |
| `harness/snsfeed.py` + `harness/snsfeed_inputs/` | CREATE | Synthesized SNS fixture generator + vendored schema template/keypair (decision 4/5) |
| `harness/cli.py`, `harness/runrecord.py` | UPDATE | `backfill-sns` command; run records link their SNS fixture (the `deploy:` precedent) |
| `fixtures/sns/` + `PROVENANCE.md` | CREATE | 11 fixtures + full provenance/deltas + live-capture path |
| `culprit/eval/replay.py` | UPDATE | Silent runs: ingest SNS fixture + `FixtureLogsProvider`; two-source dedup case |
| `culprit/eval/score.py`, `driver.py`, `cli.py` | UPDATE | New classes (per-source top-k, dedup, gated runbook/similar sections); report format |
| `culprit/eval/runbook_labels.yaml` | CREATE | fault_id → expected runbook (scorer-only; decision 12) |
| `docs/aws/culprit-readonly-policy.json` | CREATE | The exact scoped IAM ask (HANDOFF §5) — ready to hand the VP |
| `docs/aws/alarms-proposal.tf` | CREATE | The alarm suite in their iac style (fixture source of truth + pitch PR #2) |
| `docs/aws/aws-access.md` | CREATE | The access ask, Mode B cross-account notes, fixture→live-capture upgrade path |
| `docs/pipeline.md` | UPDATE | M3 sections: SNS ingest, providers, new eval classes/denominators |
| `tests/test_runbooks.py`, `test_impact.py`, `test_diagnosis.py`, `test_similar.py`, `test_sns_verify.py`, `test_ingest_sns.py`, `test_cloudwatch.py`, `test_snsfeed.py` | CREATE | TDD per task (env/network-gated where secret-dependent) |
| `tests/test_corpus.py`, `test_ranking.py`, `test_correlation.py`, `test_eval.py` | UPDATE | SNS-fixture invariants; frameless ranking; cross-source dedup; expanded eval |

## Tasks

### Task 1: Runbook corpus + loader (zero AWS)
- **Action**: Author the 8–12 runbooks (catalog above) as `runbooks/*.md` with validated frontmatter (`id`, `title`, `summary`, `failure_mode`, `symptoms`, `checks`, `steps`, `rollback`), grounded in the fork's `iac/` (RDS/ElastiCache/ECS/ALB/Cognito/CloudFront) and the M1 fault manifest's real symptoms. `culprit/runbooks.py` loads + validates (unique ids, required fields, no executable steps — offer-only language).
- **Mirror**: `harness/manifest.py` schema-validation style; self-describing contracts (`culprit/models.py:1-7`).
- **Validate**: `uv run pytest tests/test_runbooks.py` — corpus loads; ids unique; every incident-producing `fault_id` in `harness/faults/manifest.yaml` has exactly one intended runbook (the Task 9 label map is authorable).

### Task 2: Runbook retrieval (prompt-pick v1) + brief section
- **Action**: `RunbookSelector` interface in `culprit/runbooks.py`; v1 impl in `culprit/llm.py` — titles+summaries in the prompt, Sonnet picks (temperature 0, output constrained to corpus ids), env-gated. `culprit/brief.py` gains "Suggested runbook (offer-only — Culprit never executes)" with title + why.
- **Mirror**: LLM gating (`culprit/pipeline.py:156-158`); inert-when-keyless (`culprit/config.py:26-51`).
- **Validate**: unit tests with a fake selector; keyless → brief omits the section; gated live test: a replayed `redis-down` incident yields the `redis-elasticache-down` runbook.

### Task 3: Impact calculator (deterministic, methodology stated)
- **Action**: `culprit/impact.py` — exact failed-request count + hedged unique-user estimate, each with a method string (decision 10). v1 sources: Sentry `issue.count`/`userCount` (already ingested). The logs-derived source (error-line counts + distinct client IPs) plugs in at Task 8 behind the same interface; live ALB metrics at AWS-grant time. Replace the inline f-string impact in `culprit/pipeline.py:153-155`; render in `culprit/brief.py`.
- **Mirror**: deterministic-what-is-countable (HANDOFF §3); M2's `issue.count` seeding.
- **Validate**: unit tests: same inputs → same numbers; every emitted number carries a method string; brief shows "~N failed requests…; est. ~U users (method: …)".

### Task 4: Diagnosis synthesizer
- **Action**: `culprit/diagnosis.py` (decision 14): deterministic hypothesis assembly (code-culprit from ranking + infra classes + explicit alternative/insufficient-evidence floor), fixed-threshold confidence bands, per-hypothesis `evidence` row-id citations. Alembic migration adds `incidents.diagnosis` jsonb; persist at analysis; Sonnet phrases narrative only; brief renders ranked hypotheses.
- **Mirror**: ranked-hypotheses-never-one-answer (HANDOFF §3); jsonb audit trail (`culprit/models.py`).
- **Validate**: unit tests over replayed recorded incidents: culprit case → top hypothesis cites the ranked culprit's evidence ids with ≥1 alternative; infra case → infra hypothesis leads; no rendering path ever asserts a single unqualified answer; migration up/down clean.

### Task 5: pgvector + similar-incident search
- **Action**: Migration: `CREATE EXTENSION vector` + `incidents.embedding`; docker-compose + CI → `pgvector/pgvector:pg17`. `culprit/similar.py`: Voyage REST via httpx (`VOYAGE_API_KEY`-gated), embed title+frames on incident open, nearest-neighbor over prior incidents, cite in brief.
- **Mirror**: env-gated integration + skipping tests; httpx client style (`culprit/github_api.py`).
- **Validate**: migrations + full suite green on the new image; keyless → inert + tests skip (ask the user for a key before skipping live validation); gated test: embeddings round-trip and a seeded near-duplicate incident is retrieved.

### Task 6: Alarm suite + synthesized SNS fixtures (the contract, before the endpoint)
- **Action**: Write `docs/aws/alarms-proposal.tf` (decision 3, their iac style) — the source of truth for alarm names/metrics/dimensions. Build `harness/snsfeed.py` + `culprit-harness backfill-sns` (decisions 4–5): vendored real SNS envelope schema + real CloudWatch alarm `Message` JSON per the fault→alarm map, recorder-envelope wrapped, deterministic opaque ids, signed with the vendored keypair, `reconstructed: true`; 11 fixtures under `fixtures/sns/`; run records link them; `fixtures/sns/PROVENANCE.md` documents every synthesized field + the live-capture path. Extend `tests/test_corpus.py` (no orphans; signatures verify; payloads never name the fault).
- **Mirror**: `harness/deployfeed.py` end-to-end (schema template, deterministic synthesis, provenance, key-parity test `tests/test_deployfeed.py`).
- **Validate**: `uv run pytest tests/test_snsfeed.py tests/test_corpus.py` — key-parity vs the vendored template; regeneration byte-stable; anti-leakage invariants green.

### Task 7: `POST /ingest/sns` + cross-source correlation
- **Action**: `culprit/sns_verify.py` (canonical string, X.509 via `SigningCertURL` with host allowlist, https-only — decision 5); `culprit/ingest/sns.py` (handshake via allowlisted `SubscribeURL` GET; Notification → Signal per decision 1; idempotent; `text/plain` body handled); wire the route in `culprit/app.py`; extend `culprit/correlation.py` with the windowed cross-source join (decision 7).
- **Mirror**: verify-then-parse ingest (`culprit/ingest/sentry.py`); 401-on-bad-signature; idempotency on `dedup_key`.
- **Validate**: replay all 11 SNS fixtures → Signals with correct fingerprint/dedup keys; tampered body → 401; disallowed `SigningCertURL`/`SubscribeURL` host → rejected; handshake test against a stubbed SubscribeURL; duplicate delivery → one row; `redis-down` Sentry + SNS fixtures → **exactly 1 incident**.

### Task 8: CloudWatch logs provider + frameless ranking
- **Action**: `culprit/cloudwatch.py` (`LogsProvider`: `Boto3LogsProvider` gated on AWS creds via `asyncio.to_thread`; `FixtureLogsProvider` over `fixtures/logs/`); `culprit/logparse.py` (middleware exception-JSON → frames; gunicorn `WORKER TIMEOUT`/OOM markers; distinct-IP + error-line counts for impact). Pipeline: stack-trace source order webhook → logs → absent (HANDOFF §4). `culprit/ranking.py`: log-frames feed the existing composite unchanged; no-frames → alarm-class diff-surface affinity with a higher abstention bar (decision 9). Impact gains the logs source (Task 3 interface).
- **Mirror**: provider-behind-interface + env-gating; the composite stays intact (HANDOFF-M2 gotcha 1 — regression-test it).
- **Validate**: unit tests per log-fixture class (a traceback-bearing log yields frames matching its Sentry twin's; `gunicorn-worker-oom`'s log yields infra markers and no code affinity → abstain path); M2's 10 Sentry-visible ranking tests still 10/10.

### Task 9: Eval expansion (the M3 numbers)
- **Action**: `culprit/eval/replay.py`: silent runs ingest their SNS fixture + run with `FixtureLogsProvider`; add the two-source dedup case. `culprit/eval/score.py`: per-source top-k classes (Sentry N=10, SNS-silent N=8, combined N=18), abstention N=3, dedup correctness, gated runbook-precision section (vs `runbook_labels.yaml`, scorer-only) and gated similar-incident section (w1/w4 sibling retrieval). Report format states every denominator.
- **Mirror**: anti-leakage (`culprit/eval/replay.py:1-9`); honest per-class N (`culprit/eval/score.py:53-89`).
- **Validate**: `uv run culprit eval` — all 22 runs replayed; per-class table correct; anti-leakage assertions extended (pipeline never reads labels; SNS payloads carry no fault identity); deterministic sections identical run-to-run.

### Task 10: AWS access pack + docs + CI
- **Action**: `docs/aws/culprit-readonly-policy.json` — the exact HANDOFF §5 ask: `logs:StartQuery/GetQueryResults/FilterLogEvents` scoped to their two log groups (`/ecs/<prefix>-django`, `redis-cache`), `cloudwatch:GetMetricData/DescribeAlarms`, `ecs:Describe*/ListTasks`, `elasticloadbalancing:Describe*`. `docs/aws/aws-access.md`: the ask, Mode B cross-account posture, and the fixture→live upgrade path (swap `FixtureLogsProvider`→`Boto3LogsProvider`, capture real SNS deliveries, re-run the eval). Update `docs/pipeline.md` (SNS env vars, providers, new denominator table). CI stays secretless-green.
- **Mirror**: `docs/pipeline.md` runbook style; HANDOFF §5's exact action list.
- **Validate**: `python -m json.tool docs/aws/culprit-readonly-policy.json`; `uv run ruff check . && uv run ruff format --check . && uv run pytest` green locally + CI; `uv run culprit eval` green.

## Validation
```bash
export PATH="$HOME/.local/bin:$PATH"
set -a; source .env; set +a
export GITHUB_TOKEN=$(gh auth token)
uv sync && uv run ruff check . && uv run ruff format --check .
docker compose up -d db && uv run culprit migrate          # pgvector image + new migration
uv run pytest                                               # offline+DB suite; gated tests need keys
uv run culprit eval                                         # 22 runs, per-class N (the M3 numbers)
uv run culprit serve --port 8010                            # POST a fixtures/sns/* envelope → brief with
                                                            # impact + runbook + diagnosis sections
```

## Risks
| Risk | Likelihood | Mitigation |
|---|---|---|
| Synthesized SNS shape drifts from real deliveries (never captured live) | Medium | Vendored real message-format schema + key-parity test (deployfeed precedent); `text/plain` + header quirks handled; PROVENANCE.md documents deltas + the personal-account live-capture path — a real capture upgrades the fixtures without touching the service |
| Frameless ranking underperforms on silent faults (thin evidence: latency alarms carry no path) | High | Separate per-class N so the Sentry-visible 10/10 is never silently diluted; log-derived frames reuse the proven composite; higher abstention bar prevents confident wrong answers; the honest number is the deliverable |
| `search-silent-zero-results` is undetectable without a synthetic canary | Certain (by design) | The canary is a first-class alarm in the proposal and its necessity is stated in the report + pitch — it's the argument *for* the instrumentation PR |
| LLM-driven runbook pick makes that eval section nondeterministic/costly | Medium | Temperature 0, ids-constrained output, gated on the key, own N; deterministic culprit/abstention eval untouched (never scores LLM output) |
| AWS access never granted (HANDOFF §7 Q2) | Medium-high | Tasks 1–5 need zero AWS; 6–9 run entirely on fixtures; providers swap behind interfaces; Task 10's policy JSON + alarms proposal make the ask concrete in the pitch |
| Cross-source windowed join merges two genuinely distinct outages | Low (single service, rare concurrency) | Release-compatibility check retained; tradeoff documented; dedup eval case pins intended behavior; revisit post-deploy |
| Voyage key absent → similar-incident inert and unvalidated | Medium | Env-gated skip per convention, but ask the user for the key before skipping live validation (project memory: never silently skip gated validations) |
| pgvector image/migration breaks local or CI DB | Low | `pgvector/pgvector:pg17` is drop-in for postgres:17; migration up/down tested; CI migrate step already exists |
| SNS endpoint becomes an SSRF/abuse surface (`SubscribeURL`/`SigningCertURL` fetches) | Medium | Strict https + amazonaws.com host allowlists on both; signature verification mandatory in live mode; topic-ARN allowlist setting |

## Forward visibility → Milestones 4/5
- `incidents.diagnosis` (hypotheses + runbook + impact snapshot) + the `jobs`/`evidence` audit trail is the **M4 postmortem PR's** input; the brief's resolve affordance is its trigger.
- `docs/aws/alarms-proposal.tf` **is** the second instrumentation PR for the pitch (HANDOFF §6); the policy JSON is the access ask; both go in front of the VP with the §7 questions.
- The M3 eval report (expanded N, per-class) is the Phase-5 headline source; corpus expansion (more distinct faults) remains the flagged pre-Phase-5 task for strengthening N.
- A real SNS capture (post-grant or personal account) upgrades `fixtures/sns/` in place — the documented deployfeed-style path.

## Acceptance
- [ ] All 22 corpus runs enter `culprit eval` with honest per-class N: top-k Sentry-visible N=10 and SNS-silent N=8 reported separately **and** combined (N=18); abstention N=3; baseline N=1; nothing deferred silently
- [ ] `POST /ingest/sns` completes the `SubscriptionConfirmation` handshake, verifies signatures (allowlisted cert host), parses Notifications idempotently — with **no change to the `signals` schema**
- [ ] Cross-source dedup: one outage firing Sentry + SNS yields exactly 1 incident/brief
- [ ] 8–12 runbooks authored for tCF's real failure modes; briefs offer (never execute) a runbook; precision scored in a gated section against scorer-only labels
- [ ] Every brief carries deterministic impact: exact request count + hedged user estimate, each with stated methodology; the LLM computes no number
- [ ] Diagnosis renders ranked hypotheses with confidence + cited evidence ids, never a single asserted answer; persisted for M4
- [ ] Similar-incident search via pgvector (gated) retrieves w1/w4 siblings in its eval section
- [ ] `docs/aws/culprit-readonly-policy.json` + `alarms-proposal.tf` + `aws-access.md` exist and match HANDOFF §5's exact ask — ready to hand the VP; zero live AWS required anywhere in M3
- [ ] Anti-leakage: the scorer remains the only ground-truth reader; SNS payloads/fixtures never name the fault; M2's ranking tests still pass untouched
- [ ] Patterns mirrored (deployfeed provenance, env-gated tests, TDD, deterministic-decides); ruff + pytest green locally and in secretless CI
