# Culprit — Milestone 2 Executive Handoff

**Date:** 2026-07-04
**Status:** ✅ **COMPLETE.** Core pipeline built test-first; live eval green; opened as a PR.
**Branch:** `feat/m2-core-pipeline` → PR [#2](https://github.com/IshanA2007/Culprit/pull/2) into `main` (CI green: lint + test). 12 commits.
**Prereq reading:** [`HANDOFF.md`](HANDOFF.md) (project vision, §4/§5), [`.claude/plans/culprit-m2-core-pipeline.plan.md`](.claude/plans/culprit-m2-core-pipeline.plan.md) (the M2 plan), [`docs/pipeline.md`](docs/pipeline.md) (service runbook), [`HANDOFF-M1.md`](HANDOFF-M1.md) (the corpus this consumes).

---

## 1. What M2 delivers (the TL;DR)

The **`culprit/` FastAPI service** — the product. It turns the M1 corpus into working software: it ingests the recorded webhook contracts, models **signals → correlation-window → incidents** in Postgres, reconstructs the deploy window pinned to the deployed SHA, gathers evidence (diffs + blame), **ranks the culprit commit or abstains**, and posts a Discord brief. A replay-based eval scores it against `runs/*.yaml`.

It is three things at once:

1. **The product** — an injected fault now produces a deduped Discord brief with a ranked culprit commit (or an honest "no code culprit") end-to-end.
2. **The resume-number generator** — `culprit eval` replays the corpus through the live pipeline and prints top-1/top-3 culprit accuracy, abstention correctness, and false-positive rate, with honest per-class N.
3. **The Milestone-3 foundation** — the schema, ingest boundary, and job table accept a new alert source (SNS/CloudWatch) with no structural change.

**Built entirely on the M1 corpus: zero AWS, zero new recording.** The `harness/` package (M1) is untouched.

---

## 2. The numbers (`culprit eval`, deterministic, live over the M1 corpus)

| Metric | Result | N |
|---|---|---|
| Culprit commit **top-1** | **10/10 (100%)** | 10 |
| Culprit commit **top-3** | **10/10 (100%)** | 10 |
| Reconstructed window == recorded window | 10/10 | 10 |
| Abstention correct ("No code culprit — looks infrastructural") | 2/2 | 2 |
| Baseline false positives (benign deploy → no brief) | 0/1 | 1 |
| Deferred to M3 (silent faults; no Sentry event) | — | 9 |
| Median time-to-brief (compute, cached fork reads) | ~0.01s | — |

**Read the N honestly.** The headline denominator is **10 Sentry-visible code faults** = 5 distinct faults × 2 window sizes. The 8 silent code faults + `gunicorn-oom` emit no Sentry event *by design* (they surface as 502s/hangs, caught by M3's SNS/CloudWatch ingest), so they produce no incident here and are counted as **deferred, never as misses**. 100% top-1 is a real result but on a small, single-corpus N with a deterministic heuristic — present it with the N, not as a universal accuracy claim. The 27%-of-incidents-are-code-bugs framing from HANDOFF §3 still governs the pitch.

---

## 3. How it works (architecture)

**One package (`culprit/`), one Postgres. The recorder's raw-envelope shape (M1) is the ingest boundary — live endpoints receive the same bytes, verify the HMAC, then parse.**

```
POST /ingest/sentry  ─┐ verify Sentry-Hook-Signature → parse event_alert/issue → Signal ─┐
POST /ingest/github  ─┘ verify X-Hub-Signature-256   → parse workflow_run     → Deploy    │
                                                                                          │  correlation window (~10 min)
                                              first Signal opens an Incident; same         ├─→ Incident (exactly 1 per outage)
                                              fingerprint within the window joins it  ◄─────┘        │
                                                                                                     │
   window = compare(previous_head_sha, release_sha).commits   ◄── deploy timeline (pinned to SHA)    │
        │                                                                                            │
   evidence: per-window-commit diffs + per-stack-frame blame @ release_sha                           │
        │                                                                                            │
   ranking (deterministic): frame-file overlap + stem affinity + error-symbol-in-diff + blame hits   │
        │  → ranked culprit  OR  abstain (infra-class error + no window overlap)                     │
        │  (Sonnet 5 writes the rationale, Haiku 4.5 summarizes — deterministic is authoritative)    │
        └─→ Discord brief (living message: post once, edit as signals join) ──────────────────────────┘
```

**Load-bearing design decisions (validated on real data, not guessed):**

- **Correlation keys on the Sentry issue *title*, not the release.** The `issue` webhook carries no `release` (only `event_alert` does), but both carry an identical `title`. So an incident groups by fingerprint (title) with release as a *compatibility* check — that's what lets a release-less `issue` collapse into its `event_alert`. Verified: title matches across EA+IS in every recorded run.
- **The window is `compare(base, release)`.** Verified set-equal to the recorded window for **all 22 runs** against the live fork. In the eval the replay seeds a prior deploy at the run's `base_sha` so `previous_head_sha == base_sha` (plan decision 5).
- **Ranking is richer than pure blame — and had to be.** Pure git-blame on stack frames (Sentry's textbook "suspect commits") scores **0 for most faults here**: the crashing frame line is usually *not* the line the fault changed (a bad template surfaces in the view frame; a dropped column surfaces in the reader). The working signal is a composite: **frame-file overlap (×3) + file-stem affinity (×2, e.g. `course_instructor.html` ~ `.py`) + error-named-symbol-in-diff (×1) + blame hits (×3)**, with comment/docstring-only diffs zeroed (a benign decoy can't crash). Ties preserve compare order (oldest first) so **the release head is never surfaced for being newest** (anti-leakage). This composite puts the labeled culprit at rank 1 in all 10 cases.
- **Abstention is a verdict, not a low score.** An infra-class error (`ConnectionError`/`OperationalError`) whose frames implicate no window commit → "No code culprit — looks infrastructural." That's exactly the `redis-down`/`db-stopped` runs.
- **Anti-leakage is sacred.** The pipeline consumes only the ingest contract + deploy feed; `culprit/eval/score.py` is the *only* reader of `is_culprit`/`ground_truth`/`culprit_sha`.

---

## 4. Live environment state (what's set up on this machine)

| Thing | State |
|---|---|
| Service DB | `docker compose up -d db` → **`culprit_m2_db`** (Postgres 17, host **:5432**, named volume `culprit_culprit_m2_pgdata`). `docker-compose.yml`. |
| Migrated schema | `uv run culprit migrate` (Alembic baseline `e989c92f40f8`) applied to the `culprit` DB. |
| Test DB | `culprit_test` — created per pytest session by `tests/conftest.py` from the `postgres` maintenance DB; kept separate so autogenerate sees a pristine `culprit` DB. |
| GitHub reads | Authenticated against the fork `IshanA2007/theCourseForum2`. Locally the token comes from **`export GITHUB_TOKEN=$(gh auth token)`** (`gh` is authenticated) — no dedicated PAT needed for dev; the deployed service will set `GITHUB_TOKEN` properly. |
| API cache | `.cache/github/` (gitignored) — immutable-SHA responses cached so the 22-run eval stays inside the rate limit. |
| `.env` (gitignored) | `SENTRY_DSN/ORG/PROJECT/URL/AUTH_TOKEN`, `SENTRY_CLIENT_SECRET`, `CULPRIT_GH_WEBHOOK_SECRET`, `GITHUB_TOKEN`, `ANTHROPIC_API_KEY`, `DISCORD_WEBHOOK_URL`, `NGROK_DOMAIN`. Source before secret-gated work: `set -a; source .env; set +a`. |
| tCF dev containers | `tcf_django` (holds host **:8000**) + `tcf_db` are up; the M2 DB deliberately avoids both ports. Never `docker rm tcf_db` (M1 note). |
| **The M1 working clone is GONE** | `.harness-work/theCourseForum2` no longer exists on this machine. M2 does **not** need it — it reads diffs/blame via the GitHub API. (Consequence: the one M1 test `test_every_recorded_sha_is_resolvable` skips locally; M2 verifies resolvability via the API instead.) |

**To run the whole thing** (fresh session):
```bash
export PATH="$HOME/.local/bin:$PATH"
set -a; source .env; set +a
export GITHUB_TOKEN=$(gh auth token)
docker compose up -d db && uv run culprit migrate
uv run pytest                          # full suite (110 pass locally, 1 M1 clone skip)
uv run culprit eval                    # the resume numbers
uv run culprit serve --port 8010       # then POST a recorded webhook and watch a brief post
```

---

## 5. Hard-won gotchas (these will bite whoever picks this up)

1. **Pure stack-frame blame is a weak culprit signal on this corpus.** The frame line ≠ the changed line for template, migration, and cross-file logic faults. If you "simplify" ranking back to blame-only, top-1 collapses to ~1/5. The composite scorer in `culprit/ranking.py` is load-bearing — keep the file/stem/symbol signals.
2. **The Sentry `issue` payload has no `release`.** Don't key correlation on release; key on the title/fingerprint (both webhooks share it) and treat release as a compatibility check.
3. **Sentry titles overflow `varchar(255)`** (the `FieldError` "Choices are: …" list). `signals.fingerprint` and `incidents.correlation_key` are `Text`. A tester caught this — that's why the baseline migration was regenerated once.
4. **Async SQLAlchemy + asyncpg hate cross-event-loop reuse.** `tests/conftest.py` builds a fresh engine per test (function scope) and does session-DB setup in `asyncio.run(...)` — don't make the engine session-scoped or you get "attached to a different loop."
5. **`gh auth token` is your dev GitHub credential.** There's no local clone fallback, so no token = unauthenticated 60 req/hr and a flaky eval. Always `export GITHUB_TOKEN=$(gh auth token)`.
6. **The eval seeds a prior deploy at `base_sha` with an *early* timestamp** so `previous_head_sha` resolves to it (the deploy-timeline lookup orders by `run_started_at`). If you change the seed timestamp to "now", the window base breaks.
7. **zsh reserves `status`** (M1 note, still true) — and **host :8000 is taken by `tcf_django`**, so the `culprit serve` smoke-check binds an alternate port.
8. **LLM output is never scored.** The eval runs with no LLM and no Discord (deterministic + reproducible + free). The LLM (`culprit/llm.py`) only phrases the brief; if you route it into the verdict, the eval numbers become nondeterministic.

---

## 6. Honest accounting — done vs. deferred

**Done:** all 10 plan tasks — scaffold + Postgres + settings, data model + Alembic baseline, signatures + Sentry ingest, GitHub deploy ingest + timeline, correlation/dedup, window reconstruction + evidence (pinned SHA), deterministic ranking + abstention + LLM rationale, Discord living-message brief + pipeline, the replay eval, and the CI Postgres service + runbook. Every task's Validate step passed before moving on; each is a commit.

**Deferred / not done (be honest about these):**
- **Silent faults (9) are out of the Sentry-driven eval** until M3's SNS/CloudWatch ingest exists — they emit no Sentry event by design. Their run records carry culprit ground truth, so they join the top-k denominator then (N grows 10 → 18+).
- **The ranker is a deterministic heuristic, not learned.** It scores 100% top-1 on N=10, but that N is small and single-corpus. Corpus expansion (more distinct faults) is the honest way to strengthen the headline — flagged as a pre-Phase-5 task in the plan's Risk table.
- **Impact is v1** — exact `issue.count` failed-request count + a hedged `userCount` estimate with the method stated. The deterministic impact *calculator* (access-log/CloudWatch-backed) is M3.
- **No SNS ingest, no runbook retrieval, no postmortem, no diagnosis synthesizer** — M3/M4 by design. `pgvector` is deferred (M3).
- **The M1 SHA-resolvability test skips locally** because the working clone is gone; M2's equivalent runs live via the API (`tests/test_window_evidence.py`).

---

## 7. Path to Milestone 3 (full diagnosis layer)

The M2 service is M3's substrate. Per [`HANDOFF.md`](HANDOFF.md) §4/§6 and the plan's forward-visibility section:

- **SNS/CloudWatch ingest** (`POST /ingest/sns`, the `SubscriptionConfirmation` handshake): `signals`/`incidents` accept a new source with no schema change, pulling the **silent faults** into the eval.
- **Runbook retrieval** (8–12 runbooks for tCF's real failure modes): lands in `culprit/llm.py` + a prompt-embedded runbook list; `pgvector` enters for "similar past incident" search.
- **Impact calculator + diagnosis synthesizer**: build on the `evidence` + `jobs` jsonb audit trail already persisted; `issue.count` seeds impact today.
- **Postmortem PR** (M4): the `brief` + resolve affordance is the hook (timeline + culprit + impact + thread → Markdown PR).

---

## 8. File map (what M2 added)

```
culprit/                     the service (the product)
  config.py                  pydantic-settings over env/.env (all secrets optional)
  db.py                      async SQLAlchemy engine/session (lazy; make_engine for tests)
  models.py                  Deploy, Signal, Incident, Evidence, Job (SQLAlchemy 2.0, jsonb)
  signatures.py              constant-time HMAC verify (Sentry hexdigest, GitHub sha256=)
  app.py                     FastAPI: /health, POST /ingest/sentry, POST /ingest/github
  ingest/sentry.py           parse event_alert/issue → Signal (idempotent on dedup_key)
  ingest/github.py           parse workflow_run "AWS Deployment" → Deploy (previous_head_sha)
  correlation.py             open/join incidents by fingerprint within the window; severity
  github_api.py              async REST+GraphQL (compare, commit, blame) + immutable cache
  deploys.py                 window = compare(previous_head, release).commits
  evidence.py                per-commit diffs + per-frame blame @ release_sha → Evidence
  ranking.py                 deterministic score + abstention (THE culprit logic)
  llm.py                     Sonnet 5 rationale + Haiku 4.5 summarize (phrasing only)
  brief.py                   render_brief + DiscordClient (living message: post then edit)
  pipeline.py                run_pipeline: incident → window → evidence → rank → brief
  eval/replay.py             replay one run (reset → seed base → ingest → pipeline)
  eval/score.py              THE only ground-truth reader; per-class N + metrics
  eval/driver.py             evaluate_all over the corpus
  cli.py                     culprit serve | migrate | eval
migrations/                  Alembic baseline (async env wired to culprit.models)
docker-compose.yml           Postgres 17 (culprit_m2_db, host :5432)
docs/pipeline.md             the service runbook
tests/                       async pytest over committed fixtures + ephemeral Postgres
  conftest.py                culprit_test DB + async db_session + ASGI client fixtures
  test_service_scaffold / test_models / test_ingest_sentry / test_ingest_github /
  test_correlation / test_window_evidence / test_ranking / test_llm / test_brief /
  test_pipeline / test_eval
.github/workflows/ci.yml     lint + test (postgres:17 service + culprit migrate)
.cache/                      gitignored GitHub API response cache
```

---

*M2 built and verified against the live fork + Anthropic + Discord, test-first, task-by-task. The service works end-to-end; the numbers it produces are anti-leakage-safe and honestly denominated. Milestone 3 is the diagnosis layer that makes the brief richer — runbooks, impact math, and the SNS path that pulls the silent faults in.*
