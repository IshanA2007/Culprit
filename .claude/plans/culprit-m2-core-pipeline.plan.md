# Plan: Culprit Milestone 2 — Core Pipeline MVP

**Source PRD**: `.claude/prds/culprit.prd.md`
**Selected Milestone**: 2 — Core pipeline MVP
**Complexity**: Large

## Summary
Build the FastAPI incident-response service that turns the M1 corpus into working software: ingest the recorded webhook contracts (`POST /ingest/sentry`, `POST /ingest/github`), model **signals → correlation-window → incidents** in Postgres, reconstruct the deploy window from the deploy feed, gather evidence pinned to the deployed SHA (GitHub diffs/blame), **rank the culprit commit or abstain**, and post a Discord brief. A replay-based eval scores top-1/top-3 culprit accuracy and abstention correctness against `runs/*.yaml` — the origin of the resume numbers. This is the first milestone that produces the product; it consumes the M1 corpus and requires **zero AWS and zero new recording**.

The critical correlation join is already verified against the corpus: **Sentry `event.release` == run `release_sha` == deploy `workflow_run.head_sha`**, so alert → deploy → pinned-SHA → window is a real, tested chain.

## Patterns to Mirror
M1 established Culprit's conventions; M2 extends them. tCF refs are for infra shape only.

| Category | Source (`file:line`) | Pattern |
|---|---|---|
| Ingest boundary | `harness/recorder/app.py:45-69` | Raw-body-first envelope (`received_at`, `source`, `resource`, `headers`, `raw_body`); the recorded bytes ARE the ingest contract — the service parses the same shape it receives live |
| Signature verify | `tests/test_corpus.py:103-124`, `harness/scrub.py:56-60` | HMAC over the raw body vs an env-injected secret (`SENTRY_CLIENT_SECRET`, `CULPRIT_GH_WEBHOOK_SECRET`); never trust an unsigned request |
| Self-describing schema | `harness/runrecord.py:24-72` | Flat dataclass-style records, `to_dict`/`load_*` round-trip, docstring states the contract; mirror for SQLAlchemy models |
| Eval cases + labels | `runs/*.yaml` (`harness/runrecord.py`) | `window` (ordered candidates, `is_culprit`), `ground_truth` ∈ {`culprit_commit`,`abstain`,`no_incident`}, `base_sha`/`release_sha` — the answer key |
| Deterministic-what-is-countable | HANDOFF §3; `harness/fork.py` | Deterministic code for scoring/windows/impact; LLM only phrases and re-ranks |
| Config/paths | `harness/config.py` | Single module of constants + env overrides; add a `pydantic-settings` layer for the service |
| Deps + tooling | `pyproject.toml:5-24`, `.github/workflows/ci.yml` | uv, py3.12, bounded ranges, ruff (`E4/E7/E9/F/I/W/UP/B/SIM`), pytest; CI = `setup-uv@v5` + `uv sync --frozen --group dev` |
| Tests over fixtures | `tests/test_corpus.py`, `tests/test_deployfeed.py` | pytest invariants over committed fixtures; env-gated network/secret tests skip cleanly in CI |
| CI DB service | tCF `.github/workflows/ci.yml` (postgres service) | Add a `postgres` service container to the `test` job (Culprit's CI has none today) |

## Architecture decisions (this milestone)

1. **The service is a new package `culprit/`, separate from `harness/`.** `harness/` = M1 tooling/corpus; `culprit/` = the product. The recorder (`harness/recorder/app.py`) is the *seed* — its raw-envelope shape becomes the ingest boundary — but the live endpoints receive raw webhook bytes directly (verify signature, then parse). Reuse, don't fork, the envelope format.
2. **Postgres is the single source of truth; SQLAlchemy 2.0 async + Alembic.** Tables: `deploys`, `signals`, `incidents`, `evidence`, `jobs`. Async engine (`asyncpg`). **No Celery/SQS** — a Postgres `jobs` table drives the in-process async loop and doubles as a replay log (HANDOFF §5). **pgvector is deferred to M3** (runbook/similar-incident search); M2 needs none.
3. **Ingest = verify-then-parse, idempotent.** `POST /ingest/sentry` verifies `Sentry-Hook-Signature`, parses `event_alert` (stack frames + release) and `issue` (count/userCount) → `Signal`. `POST /ingest/github` verifies `x-hub-signature-256`, parses `workflow_run` → `Deploy` (this is **not** a trigger — it keeps the deploy timeline current, HANDOFF §4). Both keyed for idempotency (Sentry event id / deploy `head_sha` + action) so replays and duplicate deliveries don't double-insert.
4. **Culprit-commit analysis runs on every incident with a recent deploy, regardless of alert source** (HANDOFF §3). All code reads are pinned to the **deployed SHA** (`release_sha` from the matched deploy), never `master` HEAD.
5. **Window reconstruction from the deploy timeline via GitHub compare — the key eval-fidelity decision.** Production has no ground-truth window; M2 derives the candidate set as `compare(previous_deploy_head, this_deploy_head).commits` (`GET /repos/<fork>/compare/{base}...{head}`). For the eval to match the recorded `window`, the replay **seeds a prior deploy at the run's `base_sha`** before ingesting the run's deploy at `release_sha`; then `compare(base_sha, release_sha).commits` == the recorded window. This makes M2's window derivation testable against `runs/*.yaml` without leaking labels.
6. **Culprit ranking is deterministic blame-overlap; the LLM only re-ranks/phrases.** Score each candidate commit by how many Sentry stack frames (`file:lineno`) blame — at `release_sha` — to that commit's diff (Sentry's "suspect commits" mechanism, HANDOFF §3). Deterministic scores decide; Sonnet 5 re-ranks ties and writes the human-facing rationale (Haiku 4.5 for cheap summarization). Never a single asserted answer — always ranked hypotheses with cited evidence.
7. **Abstention is a first-class output**, not a low score. Abstain when (a) no candidate's blame-overlap clears a confidence threshold, or (b) the signal is infrastructural — a `ConnectionError`/`OperationalError` flood whose frames don't overlap any window commit (Redis/DB down over a benign deploy). Output: "No code culprit — looks infrastructural," with the reason. This is what the infra runs test (HANDOFF §3, PRD Success Metrics).
8. **Correlation window (~10 min) dedups signals into one incident.** The first qualifying signal opens an incident immediately (speed is the product); later signals sharing the correlation key (release + fingerprint family) within the window join it and raise severity — **exactly one incident/brief per outage** (PRD Risk). The brief is a living message updated as signals join.
9. **Anti-leakage in the eval is sacred.** The pipeline consumes **only** the ingest contract (fixture `raw_body` + headers) and the deploy feed; it never reads `is_culprit`/`ground_truth`/`culprit_sha`. Those are used **only** by the scorer, after ranking. The replay asserts M2's reconstructed window == the recorded window and that ranking never sees a label.
10. **Replay-based eval is the resume-number source.** `culprit eval` replays each `runs/*.yaml` through the live pipeline and reports top-1/top-3, abstention correctness, false-positive rate, and time-to-brief, with **honest per-class N** (Sentry-scoreable only: 5 code faults × 2 windows = 10 culprit cases + 2 Sentry-visible abstentions + 1 baseline; the 4 silent code faults + `gunicorn-oom` join at M3, stated explicitly).

## Files to Change

| File | Action | Why |
|---|---|---|
| `pyproject.toml` | UPDATE | Add deps: `sqlalchemy[asyncio]`, `asyncpg`, `alembic`, `pydantic-settings`, `anthropic`, `python-dateutil`; dev: `pytest-asyncio`. Add `culprit` to wheel packages; add a `culprit` console script |
| `docker-compose.yml` | CREATE | Local Postgres 17 for the service (mirrors tCF's pg version); optional service run target |
| `culprit/__init__.py` | CREATE | Package root |
| `culprit/config.py` | CREATE | `pydantic-settings` Settings: `DATABASE_URL`, `SENTRY_CLIENT_SECRET`, `CULPRIT_GH_WEBHOOK_SECRET`, `GITHUB_TOKEN`, `GITHUB_REPO` (fork), `DISCORD_WEBHOOK_URL`, `ANTHROPIC_API_KEY`, `CORRELATION_WINDOW_SECONDS=600` |
| `culprit/db.py` | CREATE | Async engine + session factory; `get_session` dependency |
| `culprit/models.py` | CREATE | SQLAlchemy models: `Deploy`, `Signal`, `Incident`, `Evidence`, `Job` |
| `migrations/` + `alembic.ini` | CREATE | Alembic baseline migration for the schema |
| `culprit/signatures.py` | CREATE | HMAC verify for Sentry (`sha256` hexdigest) and GitHub (`sha256=` prefix) over raw body |
| `culprit/app.py` | CREATE | FastAPI app: `/health`, `POST /ingest/sentry`, `POST /ingest/github` (verify → parse → persist → maybe-open-incident) |
| `culprit/ingest/sentry.py` | CREATE | Parse `event_alert`/`issue` payloads → `Signal` (fingerprint, release, frames, count, users, kind) |
| `culprit/ingest/github.py` | CREATE | Parse `workflow_run` → `Deploy` (head_sha, conclusion, run_started_at/updated_at) |
| `culprit/correlation.py` | CREATE | Open/join incidents by correlation key within the window; dedup + severity |
| `culprit/github_api.py` | CREATE | httpx client: `compare(base,head)`, commit diffs, blame (GraphQL) / file-at-SHA; rate-limit aware |
| `culprit/deploys.py` | CREATE | Deploy timeline + window reconstruction (`compare(prev_head, release)`) |
| `culprit/evidence.py` | CREATE | Gather diffs + per-frame blame pinned to `release_sha`; persist `Evidence` |
| `culprit/ranking.py` | CREATE | Deterministic blame-overlap scoring + abstention rules; returns ranked candidates or `abstain` |
| `culprit/llm.py` | CREATE | Anthropic wrapper (Sonnet 5 re-rank/rationale, Haiku 4.5 summarize); deterministic scores are authoritative |
| `culprit/brief.py` | CREATE | Render + post the Discord brief (living message: post then edit) |
| `culprit/pipeline.py` | CREATE | Orchestrates incident → window → evidence → rank → brief (the loop) |
| `culprit/eval/replay.py` | CREATE | Replay a `runs/*.yaml` through the pipeline against the committed fixtures |
| `culprit/eval/score.py` | CREATE | top-1/top-3, abstention correctness, FP rate, time-to-brief; honest per-class N |
| `culprit/cli.py` | CREATE | `culprit serve` (uvicorn), `culprit eval`, `culprit migrate` |
| `tests/test_*.py` (service) | CREATE | Async pytest over the committed fixtures (ingest, correlation, window, ranking, eval, signatures) |
| `.github/workflows/ci.yml` | UPDATE | Add a `postgres:17` service to the `test` job; run migrations before pytest |
| `docs/pipeline.md` | CREATE | Service runbook: env, `culprit serve`, replaying the corpus, the eval-denominator rules |

## Data model (schema sketch)

- **`deploys`**: `id`, `head_sha` (unique), `previous_head_sha` (nullable), `branch`, `conclusion`, `run_started_at`, `updated_at`, `raw` (jsonb). Window = `compare(previous_head_sha, head_sha)`.
- **`signals`**: `id`, `source` (`sentry`), `kind` (`event_alert`/`issue`), `dedup_key` (Sentry event/issue id), `release`, `fingerprint`, `frames` (jsonb: file/lineno/function), `count`, `users`, `received_at`, `incident_id` (fk), `raw` (jsonb). `dedup_key` unique for idempotency.
- **`incidents`**: `id`, `opened_at`, `status` (`open`/`resolved`), `release`, `correlation_key`, `severity`, `deploy_id` (fk), `verdict` (`culprit`/`abstain`), `ranked` (jsonb: [{sha, score, reason}]), `brief_message_id`.
- **`evidence`**: `id`, `incident_id`, `commit_sha`, `kind` (`diff`/`blame`), `payload` (jsonb), `cited` (bool).
- **`jobs`**: `id`, `incident_id`, `type`, `status`, `attempts`, `payload`, `created_at`, `finished_at` — the async loop's durable queue + replay log.

## Tasks

### Task 1: Service scaffold + Postgres + settings
- **Action**: Add deps (decision 2); `culprit/` package; `culprit/config.py` (`pydantic-settings`); `culprit/db.py` (async engine/session); `docker-compose.yml` (Postgres 17); Alembic init. `culprit/app.py` with `/health` only.
- **Mirror**: `harness/config.py` constants + env overrides; `pyproject.toml` bounded deps.
- **Validate**: `uv sync`; `docker compose up -d db`; `uv run culprit migrate` (alembic upgrade head) clean; `curl -fs localhost:8000/health` → ok.

### Task 2: Data model + migration
- **Action**: `culprit/models.py` (all five tables, decision-2 schema) + Alembic baseline migration.
- **Mirror**: `harness/runrecord.py` self-describing style; jsonb `raw` columns preserve the full payload (M3/audit).
- **Validate**: `alembic upgrade head` then `downgrade base` clean; a round-trip test inserts + reads each model.

### Task 3: Signatures + Sentry ingest
- **Action**: `culprit/signatures.py` (HMAC verify, constant-time); `culprit/ingest/sentry.py` (parse `event_alert` frames+release, `issue` count/users → `Signal`, idempotent on `dedup_key`); wire `POST /ingest/sentry` (401 on bad/missing signature).
- **Mirror**: `tests/test_corpus.py:103-124` verification; recorder raw-body handling (latin-1).
- **Validate**: replay every committed `event_alert`/`issue` fixture → one `Signal` each, `release` + `frames` populated; tampered body → 401; duplicate delivery → no second row. `SENTRY_CLIENT_SECRET`-gated (skip in CI without it, like M1).

### Task 4: GitHub deploy ingest + deploy timeline
- **Action**: `culprit/ingest/github.py` (parse `workflow_run` → `Deploy`); `POST /ingest/github` verifies `x-hub-signature-256`; set `previous_head_sha` from the prior deploy on the branch.
- **Mirror**: `harness/deployfeed.py` payload shape; `test_deploy_webhook_signatures_verify`.
- **Validate**: replay the 22 committed `workflow_run` fixtures → 22 `Deploy` rows, `head_sha == release_sha`; signature verifies against `CULPRIT_GH_WEBHOOK_SECRET`.

### Task 5: Correlation + incident model (dedup)
- **Action**: `culprit/correlation.py` — first qualifying `Signal` opens an `Incident`; signals with the same correlation key (release + fingerprint family) within `CORRELATION_WINDOW_SECONDS` join it; severity from count/users.
- **Mirror**: deterministic, testable (HANDOFF §3).
- **Validate**: two same-release signals seconds apart → 1 incident; two far apart → 2; the `event_alert` + `issue` for one run collapse into one incident (not two briefs).

### Task 6: Window reconstruction + evidence (pinned SHA)
- **Action**: `culprit/github_api.py` (compare/diff/blame via httpx, `GITHUB_TOKEN`, rate-limit aware); `culprit/deploys.py` (`window = compare(previous_head, release).commits`); `culprit/evidence.py` (per-frame blame at `release_sha` + candidate diffs → `Evidence`). Eval seeds a prior deploy at `base_sha` (decision 5).
- **Mirror**: reads pinned to the deployed SHA (HANDOFF §4); network-gated tests skip in CI.
- **Validate**: for a recorded run (prior deploy seeded at `base_sha`), reconstructed window commit set **==** the run's `window` SHAs; diffs + blame fetched for each; all SHAs resolvable on the fork.

### Task 7: Culprit ranking + abstention
- **Action**: `culprit/ranking.py` — score candidates by stack-frame→commit blame overlap at `release_sha`; abstain on low confidence or infra signature (decision 7); `culprit/llm.py` re-ranks ties + writes rationale (deterministic scores authoritative).
- **Mirror**: ranked hypotheses + confidence, never one asserted answer (HANDOFF §3).
- **Validate**: on the 5 Sentry code faults × 2 windows the labeled culprit is in top-3 (and the release-head decoy is never chosen merely for being newest); `redis-down`/`db-stopped` → abstain "infrastructural"; ranking never reads `is_culprit`.

### Task 8: Discord brief
- **Action**: `culprit/brief.py` + `culprit/pipeline.py` — render incident → brief (ranked culprit **or** abstention + cited evidence + a `~N failed requests` line + resolve affordance), post via Discord webhook, edit on new signals (living message).
- **Mirror**: HANDOFF §4 brief; impact = exact request count + hedged user estimate with method (v1 from `issue.count`).
- **Validate**: brief posts to a test webhook; a culprit brief cites the commit + frames; an abstention brief reads "No code culprit — looks infrastructural"; a second signal edits (not re-posts) the message.

### Task 9: Eval harness (the resume numbers)
- **Action**: `culprit/eval/replay.py` (per run: reset state, seed prior deploy at `base_sha`, ingest the run's deploy + Sentry fixtures, run the pipeline) + `culprit/eval/score.py` (top-1/top-3, abstention correctness, FP rate, time-to-brief) + `culprit eval` CLI → a report table.
- **Mirror**: `runs/*.yaml` as eval cases; anti-leakage (decision 9); honest per-class N (decision 10).
- **Validate**: `culprit eval` green; report states N per class (10 culprit / 2 abstain / 1 baseline; silent → M3); reconstructed windows == recorded; the scorer, not the pipeline, is the only reader of ground truth.

### Task 10: Service tests + CI + runbook
- **Action**: async pytest suite (ingest, correlation, window, ranking, eval, signatures) over committed fixtures + an ephemeral Postgres; `.github/workflows/ci.yml` gains a `postgres:17` service and a migrate step; `docs/pipeline.md`.
- **Mirror**: tCF CI postgres service; M1's env-gated skips.
- **Validate**: `uv run ruff check . && uv run ruff format --check . && uv run pytest` green locally + CI.

## Validation
```bash
# Service repo
uv sync && uv run ruff check . && uv run ruff format --check .
docker compose up -d db && uv run culprit migrate
uv run pytest
# End-to-end against the M1 corpus (the milestone's definition of done)
uv run culprit eval            # top-1/top-3, abstention, FP, time-to-brief (honest N per class)
uv run culprit serve &         # then replay a fixture and watch a Discord brief post
```

## Risks
| Risk | Likelihood | Mitigation |
|---|---|---|
| Window reconstruction diverges from the recorded window (base-SHA seeding wrong) | Medium | Decision 5 seeds the prior deploy at `base_sha`; Task 6 asserts set-equality vs `runs/*.yaml` before any ranking work |
| Blame-based ranking underperforms on multi-file/refactor decoys | Medium | Deterministic overlap + LLM re-rank; abstention threshold prevents confident wrong answers; eval measures it honestly |
| Thin eval N (5 distinct Sentry code faults × 2 windows) weakens headline numbers | Medium | Report per-class N honestly now; flag corpus expansion as a pre-Phase-5 task (rebuild the M1 stack to add distinct faults) |
| GitHub API rate limits during evidence gathering | Low | Authenticated `GITHUB_TOKEN`; cache compare/blame per SHA; reads are on a public repo |
| Postgres async correctness (sessions, idempotency races) | Medium | `pytest-asyncio` integration tests; unique `dedup_key`/`head_sha` constraints; transactional test isolation |
| LLM nondeterminism pollutes eval | Medium | Deterministic scores decide culprit/abstain; LLM only phrases/re-ranks ties; eval scores the deterministic verdict |
| Secrets sprawl (Sentry, GitHub, Discord, Anthropic) | Low | All via `pydantic-settings` from env/`.env` (gitignored); tests skip when a secret is absent (M1 pattern) |

## Forward visibility → Milestone 3
- `signals`/`incidents` accept a new source with no schema change → **SNS/CloudWatch ingest** (`POST /ingest/sns`, the `SubscriptionConfirmation` handshake) pulls the **silent faults** into the eval (top-k N grows from 10 → 18+).
- `evidence` + `jobs` (jsonb payloads) already hold the audit trail the **diagnosis synthesizer** and **impact calculator** build on; `issue.count` seeds impact today.
- `culprit/llm.py` + a prompt-embedded runbook list is where **runbook retrieval** (8–12 runbooks) lands; `pgvector` enters for similar-incident search.
- The `brief` + resolve affordance is the hook for the **M4 postmortem PR** (timeline + culprit + impact + thread).

## Acceptance
- [ ] All tasks complete; validation commands pass (ingest → incident → window → rank → Discord brief end-to-end on a recorded run)
- [ ] `POST /ingest/sentry` + `POST /ingest/github` verify signatures and parse the committed fixtures idempotently
- [ ] Correlation window yields exactly one incident/brief per outage; `event_alert` + `issue` for a run collapse into one
- [ ] Reconstructed deploy window == the recorded `window` for every run; all reads pinned to `release_sha`
- [ ] Culprit in top-3 for the Sentry-visible code faults; `redis-down`/`db-stopped` abstain "infrastructural"; ranking never reads ground-truth labels
- [ ] `culprit eval` reports top-1/top-3, abstention correctness, FP rate, time-to-brief with honest per-class N (silent-fault deferral to M3 stated)
- [ ] Patterns mirrored from M1 (`harness/*`) + tCF toolchain, not reinvented; ruff + pytest green in CI
