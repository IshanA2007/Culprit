# Culprit — Core Pipeline (Milestone 2) Runbook

The `culprit/` package is the product: it ingests the M1 webhook contracts,
models **signals → correlation-window → incidents** in Postgres, reconstructs the
deploy window pinned to the deployed SHA, ranks the culprit commit (or abstains),
and posts a Discord brief. A replay-based eval scores it against `runs/*.yaml`.

`harness/` (M1) is separate — it built and recorded the corpus this consumes.

## Architecture (the loop)

```
POST /ingest/sentry   ─┐  verify HMAC → parse → Signal ─┐
POST /ingest/github   ─┘  verify HMAC → parse → Deploy   │  correlation window (~10 min)
                                                         ├─→ Incident (dedup: 1 per outage)
                                                         │        │
   window = compare(previous_head, release).commits  ◄──┘        │
        │  (reads pinned to the deployed SHA)                     │
   evidence: per-commit diffs + per-frame blame @ release_sha     │
        │                                                         │
   ranking: deterministic blame/diff-overlap score → culprit or abstain
        │  (LLM only phrases the rationale; deterministic is authoritative)
        └─→ Discord brief (living message: post once, edit as signals join)
```

Postgres is the single source of truth (tables `deploys`, `signals`,
`incidents`, `evidence`, `jobs`). No Celery/SQS — the `jobs` table is the
in-process queue and replay log. pgvector is deferred to M3.

## Environment

Secrets live in `.env` (gitignored). `pydantic-settings` (`culprit/config.py`)
reads them; every secret is optional — an absent one makes that integration inert
and its tests skip (the M1 convention), so the deterministic pipeline runs with
none.

| Var | Used for |
|---|---|
| `DATABASE_URL` | Postgres (async, asyncpg). Defaults to the docker-compose DB. |
| `SENTRY_CLIENT_SECRET` | verify `Sentry-Hook-Signature` on `/ingest/sentry` |
| `CULPRIT_GH_WEBHOOK_SECRET` | verify `X-Hub-Signature-256` on `/ingest/github` |
| `GITHUB_TOKEN` | authenticated fork reads (compare/diff/blame). Locally: `export GITHUB_TOKEN=$(gh auth token)`. |
| `GITHUB_REPO` | the fork (`IshanA2007/theCourseForum2`) |
| `ANTHROPIC_API_KEY` | Sonnet 5 rationale + Haiku 4.5 summarize (LLM only phrases) |
| `DISCORD_WEBHOOK_URL` | post/edit the brief |
| `CORRELATION_WINDOW_SECONDS` | dedup window (default 600) |
| `VOYAGE_API_KEY` | embeddings for similar-incident search (M3, gated; absent → inert) |
| `SNS_SIGNATURE_STRICT` | require a valid SNS X.509 signature on `/ingest/sns` (default true) |
| `SNS_SIGNING_CERT_PATH` | dev/fixture cert for offline SNS verify; unset in prod → fetch `SigningCertURL` under the `sns.<region>.amazonaws.com` allowlist |
| `SNS_ALLOWED_TOPIC_ARNS` | comma-separated TopicArn allowlist (empty → any) |
| `AUTORUN_PIPELINE` | ingest routes run the pipeline + post the brief in the background (default false; on for the serve smoke-check) |

Source before secret-gated work: `set -a; source .env; set +a`.

## Local setup

```bash
export PATH="$HOME/.local/bin:$PATH"      # uv
uv sync                                    # install deps
docker compose up -d db                    # Postgres 17 on :5432
uv run culprit migrate                     # alembic upgrade head
curl -fs localhost:8000/health             # -> {"status":"ok"}   (if :8000 free)
```

## Running the service

```bash
uv run culprit serve --port 8000           # uvicorn (FastAPI app)
# then deliver a recorded webhook to POST /ingest/sentry|github and watch a brief post
```

## The eval (the resume numbers)

```bash
set -a; source .env; set +a
export GITHUB_TOKEN=$(gh auth token)
uv run culprit eval          # table report
uv run culprit eval --json   # machine-readable
```

`culprit eval` replays every `runs/*.yaml` through the live pipeline: reset state,
**seed a prior deploy at the run's `base_sha`**, ingest the run's deploy +
Sentry fixtures, run the pipeline, then score. **Anti-leakage is sacred**: the
pipeline consumes only the ingest contract (`raw_body` + headers) and the deploy
feed — never `is_culprit`/`ground_truth`/`culprit_sha`. `culprit/eval/score.py`
is the *only* reader of ground truth.

### Honest per-class N (what "top-3 of what?" means)

The corpus is 22 runs. M3's SNS/CloudWatch ingest pulls in the silent faults, so
**all 22 are now scored** (nothing deferred). Sentry-visible and SNS-silent top-k
are reported **separately AND combined** — M2's 10/10 is never silently diluted:

| Class | N | What is measured |
|---|---|---|
| Culprit — Sentry-visible code faults | 10 | top-1 / top-3 vs the recorded window (frame path) |
| Culprit — SNS-silent code faults | 8 | top-1 / top-3 (frameless: log-frames + alarm-class diff affinity) |
| Culprit — combined | 18 | the two above, denominated together |
| Abstention (infra faults) | 3 | redis-down, db-stopped, gunicorn-oom → "No code culprit — looks infrastructural" |
| Baseline (benign deploy) | 1 | produces no incident (false-positive anchor) |
| Cross-source dedup (Sentry + SNS → 1 incident) | 2 | redis-down, db-stopped fire both feeds → exactly one incident |
| Runbook precision (GATED, LLM) | 21 | correct runbook offered vs scorer-only `eval/runbook_labels.yaml` |
| Similar-incident retrieval (GATED, Voyage) | ~9 | a fault's w4 sibling retrieves its w1 via pgvector |

The gated sections run only with their key present (`uv run culprit eval`;
`--no-gated` for the deterministic headline only). The deterministic verdict is
never scored by the LLM. Silent-fault accuracy is whatever it honestly is.

## Tests

```bash
uv run ruff check . && uv run ruff format --check . && uv run pytest
```

Tests run against an ephemeral `culprit_test` database (created per session by
`tests/conftest.py`, kept separate from the migrated `culprit` DB). Secret- and
network-gated tests (signatures, live fork reads, LLM, Discord) skip when the
corresponding secret is absent — so **CI** (which has no secrets) runs the offline
+ DB suite, while **locally** (with `.env` + a GitHub token) the full live suite
runs. CI adds a `postgres:17` service and a migrate step (`.github/workflows/ci.yml`).

## Milestone 3 — the diagnosis layer

M3 extends the same service (no `signals` schema change — an alarm maps onto
`source="cloudwatch"`, `kind="alarm"`, `dedup_key="sns:<MessageId>"`,
`fingerprint=<AlarmName>`, `frames=[]`). Additive columns only: `incidents.diagnosis`
(jsonb) and `incidents.embedding` (pgvector). The Postgres image is now
`pgvector/pgvector:pg17` (docker-compose + CI).

- **`POST /ingest/sns`** — the `SubscriptionConfirmation` handshake (SSRF-guarded
  `SubscribeURL` GET), verify-then-parse Notifications (genuine SNS X.509 signature,
  `culprit/sns_verify.py`), idempotent on `MessageId`. Dispatches on the
  `x-amz-sns-message-type` header (the SNS `text/plain` gotcha). Cross-source dedup:
  an alarm within the window joins an open Sentry incident (single-service scope).
- **Providers** (`culprit/cloudwatch.py`, `LogsProvider`): `FixtureLogsProvider`
  over `fixtures/logs/` (offline eval/demo) and `Boto3LogsProvider` (live, gated on
  AWS creds). Stack-trace source order: webhook frames → logs → absent. `culprit/logparse.py`
  turns the middleware exception JSON into fork-relative frames the ranker reuses.
- **Ranking** stays the M2 composite for frame-ful incidents (log-frames included);
  frameless silent faults use alarm-class diff-surface affinity with a higher
  abstention bar (`rank_frameless`).
- **Runbooks** (`runbooks/*.md`, offer-only): the LLM picks one from titles+summaries
  (temp-0, ids-constrained), gated on `ANTHROPIC_API_KEY`. **Impact** (`culprit/impact.py`)
  states its methodology on every number. **Diagnosis** (`culprit/diagnosis.py`) renders
  ranked hypotheses with confidence + cited evidence ids, persisted to `incidents.diagnosis`
  (the M4 postmortem input). **Similar incidents** via pgvector + Voyage (gated).

**AWS: zero live dependency in M3.** SNS deliveries are synthesized shape-faithful
fixtures (`fixtures/sns/`, `harness/snsfeed.py`, signed by a vendored keypair —
`fixtures/sns/PROVENANCE.md`). The live swap is documented in
[`docs/aws/aws-access.md`](aws/aws-access.md) with the exact read-only IAM ask
([`culprit-readonly-policy.json`](aws/culprit-readonly-policy.json)) and the alarm
suite ([`alarms-proposal.tf`](aws/alarms-proposal.tf)).

## Milestone 4 — the postmortem generator

M4 turns a **resolved** incident into a **postmortem Markdown PR** on the fork —
Culprit drafts, a human merges (see [`docs/postmortems.md`](postmortems.md)). No
`signals` schema change; additive only: `incidents.resolved_at/fixing_sha/
resolution_source` + a new `postmortems` table (one row per incident — idempotent).

- **Resolution** (`culprit/resolution.py`) — one core reached by three triggers:
  operator (`POST /incidents/{id}/resolve`, `culprit resolve`), SNS `ALARM→OK`
  auto-detect (on `/ingest/sns`), and a signed Discord `/resolve` interaction
  (`POST /discord/interactions`, Ed25519). Captures the fixing commit from the
  deploy feed (or honest *none* — infra remediation).
- **Assembly** (`culprit/postmortem.py`) — deterministic from `incidents.diagnosis`
  + the deploy/signal timeline + the impact snapshot + the (gated) Discord thread
  (`culprit/discord_read.py`); the LLM phrases the Summary only.
- **Write path** (`culprit/github_app.py`) — the ONE write permission: a GitHub App
  creates a branch + `postmortems/YYYY-MM-DD-slug.md` + a PR. **No merge call
  exists.** Gated/inert → dry-run by default. See [`docs/github-app.md`](github-app.md).
- **Eval** — `culprit eval` adds a deterministic **postmortem-completeness** section
  (N=21, dry-run) plus gated narrative-fidelity and a gated live-PR test.

| Var (M4, all optional/inert) | Used for |
|---|---|
| `DISCORD_PUBLIC_KEY` | verify `/discord/interactions` (the `/resolve` command) |
| `DISCORD_BOT_TOKEN` / `DISCORD_INCIDENT_CHANNEL_ID` | read the incident chat thread (read-scoped) |
| `GITHUB_APP_ID` / `GITHUB_APP_PRIVATE_KEY`(`_PATH`) / `GITHUB_APP_INSTALLATION_ID` | the postmortem PR write path |
| `POSTMORTEMS_REPO` / `POSTMORTEMS_BASE_BRANCH` / `POSTMORTEM_DRY_RUN` | where + whether to open the PR (dry-run default) |
```

