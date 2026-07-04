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

The corpus is 22 runs. Only the Sentry-visible ones are scoreable by M2:

| Class | N | What is measured |
|---|---|---|
| Culprit commit (Sentry code faults) | 10 | top-1 / top-3 vs the recorded window |
| Abstention (infra faults) | 2 | emits "No code culprit — looks infrastructural" |
| Baseline (benign deploy) | 1 | produces no incident (false-positive anchor) |
| Deferred to M3 | 9 | silent faults (8 code + gunicorn-oom): **no Sentry event by design** → no incident here. Their run records carry culprit truth; they join the eval when M3's SNS/CloudWatch ingest exists. |

The 9 deferred runs are **not** counted as misses — publish N per class.

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

## Forward to Milestone 3

`signals`/`incidents` take a new source with no schema change → **SNS/CloudWatch
ingest** pulls the silent faults into the eval (N grows 10 → 18+). `evidence` +
`jobs` already hold the audit trail the diagnosis synthesizer and impact
calculator build on. `culprit/llm.py` + a prompt-embedded runbook list is where
runbook retrieval lands; pgvector enters for similar-incident search.
```

