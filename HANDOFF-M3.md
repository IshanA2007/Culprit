# Culprit — Milestone 3 Executive Handoff

**Date:** 2026-07-04
**Status:** ✅ **COMPLETE.** Full diagnosis layer built test-first; honest eval green; live serve smoke-check passed; opened as a PR.
**Branch:** `feat/m3-diagnosis-layer` → PR [#3](https://github.com/IshanA2007/Culprit/pull/3) into `main` (CI green: lint + secretless test). 11 commits (10 tasks + 1 honesty fix), one per task.
**Prereq reading:** [`HANDOFF.md`](HANDOFF.md) (project vision, §4/§5), [`.claude/plans/culprit-m3-diagnosis-layer.plan.md`](.claude/plans/culprit-m3-diagnosis-layer.plan.md) (the M3 plan), [`HANDOFF-M2.md`](HANDOFF-M2.md) (the service this extends), [`docs/pipeline.md`](docs/pipeline.md) (the runbook, now with the M3 sections).

---

## 1. What M3 delivers (the TL;DR)

M3 turns the M2 pipeline into the **full diagnosis layer** — the brief goes from
"here's the culprit commit" to a complete incident report — and, critically,
**pulls the silent faults into the eval** so the corpus is scored end-to-end.

Every brief now carries: the ranked culprit (or an honest abstention), **ranked
diagnosis hypotheses** with confidence + cited evidence ids, a **deterministic
impact line** with its methodology stated, an **offer-only runbook** the model
picked from a 12-runbook corpus, and **similar past incidents** via pgvector. A
new **`POST /ingest/sns`** ingests CloudWatch alarms (silent faults + infra), and
the diagnosis is persisted to `incidents.diagnosis` — the M4 postmortem's input.

It is three things at once:

1. **The richer product** — an incident now produces a full brief (culprit +
   diagnosis + impact + runbook + similar incidents), and the SNS path catches the
   app-too-dead-for-Sentry failures M2 couldn't see.
2. **The honest resume-number generator** — `culprit eval` now scores **all 22
   runs** with per-source top-k (Sentry-visible **and** SNS-silent, reported
   separately and combined), plus gated runbook-precision and similar-incident
   sections.
3. **The Milestone-4 foundation** — `incidents.diagnosis` (hypotheses + offered
   runbook + impact snapshot) + the `jobs`/`evidence` audit trail is exactly the
   postmortem PR's input; the brief's resolve affordance is its trigger.

**Zero live AWS anywhere.** Tasks 1–5 touch no AWS shape; the SNS/CloudWatch path
runs entirely on shape-faithful synthesized fixtures.

---

## 2. The numbers (`culprit eval`, deterministic, over all 22 runs)

| Metric | Result | N |
|---|---|---|
| Culprit — **Sentry-visible** code faults, top-1 / top-3 | **10/10 / 10/10 (100%)** | 10 |
| Culprit — **SNS-silent** code faults (frameless), top-1 / top-3 | **4/8 (50%) / 5/8 (62%)** | 8 |
| Culprit — **combined**, top-1 / top-3 | **14/18 (78%) / 15/18 (83%)** | 18 |
| Reconstructed window == recorded (Sentry) | 10/10 | 10 |
| Abstention correct ("No code culprit — looks infrastructural") | 3/3 | 3 |
| Baseline false positives (benign deploy → no brief) | 0/1 | 1 |
| Cross-source dedup (Sentry + SNS → exactly 1 incident) | 2/2 | 2 |
| Runbook precision (GATED, LLM; temp-0, ids-constrained) | 14/21 (67%) | 21 |
| Similar-incident retrieval (GATED, pgvector/Voyage) | 7/9 (78%) | 9 |
| Median time-to-brief (compute) | ~0.02s | — |

**Read the N honestly.** The M2 headline (10/10 Sentry-visible) is **preserved,
not diluted** — it is reported separately. The **SNS-silent number is the honest
one**: a latency alarm names no file, so the frameless path finds ~half the silent
faults and correctly abstains on the rest. Nothing is deferred anymore — 10 + 8 +
3 + 1 = 22.

**One honesty fix worth knowing about (M3 commit `f2de2a0`):** the frameless
ranker populates its ranked list *even when it abstains*, so an early first draft
of the scorer credited top-k whenever the labeled SHA happened to sit positionally
early in a zero-scored list — inflating SNS-silent to 6/8–8/8. Top-k is now gated
on an actual `culprit` verdict; an abstention is never a hit. That is why the
honest number is 4/8–5/8, and it is the number to quote.

---

## 3. How it works (architecture)

The M2 loop is unchanged; M3 adds the SNS feed, the log fallback, and the four
brief sections. **The `signals` schema did not change** — a CloudWatch alarm maps
onto it as `source="cloudwatch"`, `kind="alarm"`, `dedup_key="sns:<MessageId>"`,
`fingerprint=<AlarmName>`, `frames=[]`. Two additive `incidents` columns:
`diagnosis` (jsonb) and `embedding` (pgvector).

```
POST /ingest/sentry ─┐ verify HMAC → Signal ─┐
POST /ingest/github ─┤ verify HMAC → Deploy   │
POST /ingest/sns    ─┘ SubscriptionConfirmation handshake; verify SNS X.509
                       (SSRF allowlist on SigningCertURL); route on the
                       x-amz-sns-message-type header (text/plain gotcha) → Signal
                                                    │  correlation window (~10 min)
   fingerprint match, else CROSS-SOURCE windowed join (an alarm joins an open
   Sentry incident — one service, one outage) ──────┼─→ Incident (1 per outage)
                                                     │
   window = compare(previous_head, release)  ◄───────┘        │
        │  (frameless SNS-only incident → the most recent deploy)
   stack-trace source order: webhook frames → LOGS → absent (HANDOFF §4)
        │  culprit/logparse.py: middleware exception JSON → fork-relative frames
   ranking: frames present → the M2 composite UNCHANGED (log-frames included);
            no frames → alarm-class diff-surface affinity + higher abstention bar
        │
   diagnosis (ranked hypotheses + confidence + cited evidence ids) → incidents.diagnosis
   impact (exact count + hedged users, method stated) · runbook (LLM picks, offer-only)
   similar incidents (Voyage embed → pgvector nearest-neighbor)
        └─→ Discord brief (living message; all sections)
```

**Load-bearing decisions (M3):**
- **Deterministic decides, LLM phrases — held everywhere.** The eval is LLM-free
  and reproducible; the LLM only writes rationale/narrative and picks the runbook
  (its own gated eval section). The deterministic verdict is never LLM-scored.
- **Frameless ranking reuses the proven composite.** A log-derived frame is
  normalized to the exact fork-relative shape the M2 ranker consumes, so silent
  faults with a logged traceback (e.g. n-plus-one) rank through the same code.
  Only truly frameless faults use the alarm-class affinity heuristic.
- **SNS signatures are real.** Genuine RSA-over-canonical-string verification runs
  offline against a vendored cert; the private key is gitignored (the deployfeed
  webhook-secret posture). Live mode fetches `SigningCertURL` under the
  `sns.<region>.amazonaws.com` allowlist.
- **Anti-leakage re-asserted at every new boundary.** `culprit/eval/score.py` is
  still the only ground-truth reader; the runbook labels are scorer-only; SNS
  payloads never name the fault (alarm names are generic infra metrics).

---

## 4. Live environment state (what's set up on this machine)

| Thing | State |
|---|---|
| Service DB | `docker compose up -d db` → **`culprit_m2_db`**, now on **`pgvector/pgvector:pg17`** (host :5432). Migrated to head (`e989…` → `a1b2…` diagnosis → `b2c3…` pgvector+embedding). |
| Test DB | `culprit_test` — conftest now runs `CREATE EXTENSION IF NOT EXISTS vector` before `create_all`. |
| `.env` additions | `VOYAGE_API_KEY` (a **native** Voyage key, prefix `pa-`). `ANTHROPIC_API_KEY` credits were refilled mid-M3. `SENTRY_*`, `CULPRIT_GH_WEBHOOK_SECRET`, `DISCORD_WEBHOOK_URL` unchanged. |
| SNS signing | `harness/snsfeed_inputs/sns_signing_cert.pem` committed (public); `sns_signing_key.pem` **gitignored**. |
| GitHub reads | Same fork `IshanA2007/theCourseForum2`; `export GITHUB_TOKEN=$(gh auth token)`. |

**To run the whole thing** (fresh session):
```bash
export PATH="$HOME/.local/bin:$PATH"
set -a; source .env; set +a
export GITHUB_TOKEN=$(gh auth token)
docker compose up -d db && uv run culprit migrate     # pgvector image + M3 migrations
uv run pytest                                          # 210 pass, 1 M1-clone skip
uv run culprit eval                                    # the M3 numbers (gated sections need the keys)
uv run culprit eval --no-gated                         # deterministic headline only
# live serve smoke-check:
AUTORUN_PIPELINE=true SNS_SIGNING_CERT_PATH="$PWD/harness/snsfeed_inputs/sns_signing_cert.pem" \
  uv run culprit serve --port 8010
# then POST a fixtures/sns/* raw_body with header x-amz-sns-message-type: Notification
```

---

## 5. Hard-won gotchas (these will bite whoever picks this up)

1. **`VOYAGE_API_KEY` must be a NATIVE Voyage key** (prefix `pa-`, from
   dash.voyageai.com), **not a MongoDB Atlas key.** `api.voyageai.com/v1/embeddings`
   returns `403 "This API key cannot access this endpoint"` for Atlas keys. Free
   tier is ~3 RPM — the eval's similar-incident section does **one batch embed** on
   purpose; don't refactor it into per-incident calls.
2. **The pgvector image swap on a reused `postgres:17` volume** trips a glibc
   collation-version mismatch that blocks `CREATE DATABASE`. Non-destructive fix:
   `ALTER DATABASE template1/postgres/culprit REFRESH COLLATION VERSION`. Fresh
   volumes (CI, a new dev) initdb cleanly — this only bites a reused volume.
3. **An abstention is never a top-k hit.** `rank_frameless` fills its ranked list
   even when it abstains; `score.py` gates top-1/top-3 on a real `culprit` verdict.
   If you "simplify" that gate away, the silent-fault number inflates on positional
   luck. (This is the M2 "don't simplify the composite" lesson, one layer up.)
4. **SNS delivers JSON with `Content-Type: text/plain`.** Route on the
   `x-amz-sns-message-type` header, never the content type.
5. **The recorded silent-fault logs are thin.** `cartesian-join`, `search-silent`,
   `bad-migration-drop-trigram`, and `gunicorn-oom` logs captured **only boot
   lines** — no `WORKER TIMEOUT`/`SIGKILL` markers, no traceback. So those faults
   go through the frameless alarm-class path (not log-frames); `n-plus-one` is the
   one silent fault whose log *does* carry a traceback. `logparse.gunicorn_markers`
   is tested against synthetic text, not the fixtures.
6. **The frameless SNS-only incident has no `release`.** `_resolve_deploy` falls
   back to the most recent deploy so the window is still pinned to a real SHA. In
   the live path (`app._run_pipeline_bg`) no `logs_provider` is wired, so a raw SNS
   POST to a fresh serve abstains (no window/logs) — the eval passes a
   `FixtureLogsProvider`, which is where the silent-fault numbers come from.
7. **All M2 gotchas still apply** (composite is load-bearing, fresh async engine
   per test, eval seeds the prior deploy at `base_sha` with an early timestamp, LLM
   output is never scored, `gh auth token` is the dev credential, zsh reserves
   `status`, host :8000 is `tcf_django` so serve binds :8010).

---

## 6. Honest accounting — done vs. deferred

**Done:** all 10 plan tasks + the honesty fix — runbook corpus + selector, impact
calculator, diagnosis synthesizer + migration, pgvector + Voyage similar-incident,
alarm proposal + 11 signed SNS fixtures (deployfeed-grade provenance), `POST
/ingest/sns` + verify + SSRF allowlist + cross-source dedup, CloudWatch logs
provider + logparse + frameless ranking, the eval expansion (per-source top-k +
dedup + gated sections), and the AWS access pack + docs. Every task's Validate
passed before the next; each is a commit. **All gated validations RAN** (Anthropic,
Voyage, Discord) — none skipped.

**Deferred / not done (be honest):**
- **The frameless heuristic is thin by design.** SNS-silent is 4/8 top-1 — a
  latency alarm carries no path, so the alarm-class affinity is a heuristic. The
  honest number is the deliverable; corpus expansion (more distinct silent faults)
  is the way to strengthen it.
- **Zero live AWS was exercised.** The SNS/CloudWatch path is proven on synthesized
  fixtures + `FixtureLogsProvider`; `Boto3LogsProvider` and live SNS verification
  are in place but untested against real AWS (gated on the VP's read-only grant —
  the swap is documented in `docs/aws/aws-access.md`).
- **No postmortem, no resolution detection, no GitHub write** — that is M4.
- **The M1 SHA-resolvability test still skips locally** (no working clone).

---

## 7. Path to Milestone 4 (postmortem generator)

M3 built M4's substrate. Per [`HANDOFF.md`](HANDOFF.md) §4/§6 and the PRD:

- **`incidents.diagnosis`** already persists the hypotheses + offered runbook +
  impact snapshot — the postmortem's structured input. The `deploys`/`evidence`/
  `jobs` audit trail holds the timeline.
- **Resolution detection** (the M4 trigger): a Discord bot command/reaction +
  auto-detect (alarm → OK, Sentry issue quiet post-deploy). Capture the **fixing
  commit** from the deploy feed.
- **The write path:** a GitHub App (the one write permission — branch + PR) opens
  `postmortems/YYYY-MM-DD-slug.md` on the fork. **Culprit drafts, humans merge —
  never publishes unilaterally** (the offer-only stance, one more time).
- **The chat thread** (read via the Discord API) is the human-narrative half of the
  postmortem, joined to the machine timeline.

---

## 8. File map (what M3 added)

```
runbooks/*.md                12 offer-only runbooks (their real failure modes)
culprit/
  runbooks.py                corpus loader/validator + RunbookSelector + coerce
  impact.py                  deterministic impact, methodology stated per number
  diagnosis.py               ranked hypotheses + confidence + evidence citations
  similar.py                 Voyage embeddings (httpx) + pgvector nearest-neighbor
  sns_verify.py              SNS X.509 verify + https/amazonaws.com SSRF allowlist
  ingest/sns.py              handshake + Notification → Signal (idempotent)
  cloudwatch.py              LogsProvider: FixtureLogsProvider + Boto3LogsProvider
  logparse.py                middleware exception JSON → frames; gunicorn markers
  (updated) config, models (+diagnosis,+embedding), app (+/ingest/sns, autorun),
            correlation (cross-source join), ranking (rank_frameless), pipeline
            (logs fallback + frameless + all sections), brief, llm, cli, eval/*
migrations/versions/         a1b2… incidents.diagnosis · b2c3… pgvector+embedding
harness/
  snsfeed.py                 synthesized SNS/CloudWatch fixture generator
  snsfeed_inputs/            vendored SNS schema template + signing cert (key gitignored)
  (updated) cli.py (backfill-sns), runrecord.py (+sns link)
fixtures/sns/                11 signed fixtures + PROVENANCE.md
culprit/eval/runbook_labels.yaml   scorer-only fault_id → runbook id
docs/aws/                    culprit-readonly-policy.json · alarms-proposal.tf · aws-access.md
docs/pipeline.md             + M3 sections (SNS ingest, providers, denominators)
docker-compose.yml, ci.yml   → pgvector/pgvector:pg17
tests/                       test_runbooks/impact/diagnosis/similar/snsfeed/
                             sns_verify/ingest_sns/cloudwatch + extended corpus/
                             ranking/correlation/eval/brief/pipeline
```

---

*M3 built and verified against the live fork + Anthropic + Voyage + Discord,
test-first, task-by-task. The service now produces a full incident brief; the
numbers it reports are anti-leakage-safe and honestly denominated — including a
proactively-fixed scoring inflation. Milestone 4 is the postmortem generator that
turns a resolved incident's persisted `diagnosis` into a Markdown PR.*
