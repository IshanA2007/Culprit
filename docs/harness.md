# Harness Runbook

End-to-end demo path + eval-denominator rules for the Culprit M1 fault-injection
harness. Doubles as the pitch-demo script.

## What it does

Forks theCourseForum2, runs it in a production-faithful Docker profile, injects
labeled faults on interleaved multi-commit deploy windows, and records the real
Sentry / GitHub webhook payloads as a labeled eval corpus. See the
[plan](../.claude/plans/culprit-m1-fault-injection-harness.plan.md).

## One-time setup

```bash
# 1. Culprit repo
uv sync

# 2. Working clone of theCourseForum2 (full history — sentry-cli set-commits
#    --local walks it) under .harness-work/, on the culprit-harness branch,
#    origin = the fork (IshanA2007/theCourseForum2).
#    .env is gitignored in tCF, so a fresh clone needs:
cp .env.example .env            # (inside the clone) shipped values suffice

# 3. Seed the harness DB (one time). The dev tCF container's DB is a prod copy;
#    snapshot it once to db/local.dump (custom format) — this is the reset source.
#    NOTE: prod-copy PII. Then bring the profile up:
docker compose -f docker-compose.harness.yml up -d db redis      # (in the clone)
#    restore local.dump into culprit_db, then run migrate to reconcile schema
docker compose -f docker-compose.harness.yml up -d --build web    # frees port 8000

# 4. Sentry (free Developer) + ngrok — see "Recording" below.
```

The dev `tcf_django` container binds host **8000**; stop it before `up web`
(the harness publishes gunicorn :80 on host :8000). Stopping preserves its data;
never `rm` it (its seed lives only in its writable layer).

## Demo path

```bash
uv run culprit-harness faults                      # list the 13-fault catalog
uv run culprit-harness run \
    template-noreversematch-instructor-card --size 4   # inject one code fault
# -> reset DB, build a 4-commit window (culprit at a non-head slot), push
#    branch+tags to the fork, run the release task, recreate web at the new
#    release, drive traffic (course pages 500 w/ NoReverseMatch), capture the
#    Sentry webhook + logs, write runs/<ts>-<id>-w4.yaml
cat runs/<ts>-template-noreversematch-instructor-card-w4.yaml
```

## Fault catalog

13 faults (`harness/faults/manifest.yaml`): 9 code (each a verified `.patch`),
3 infra (`docker_action` against `culprit_redis`/`culprit_db`/`culprit_web`),
1 benign-deploy baseline. `culprit-harness faults` prints them with class,
ground-truth label, expected signal, and auth/silent flags.

## Eval-denominator rules (what counts as a scoreable case)

- **One scoreable case = one `(fault × window-config)` pair.** Each code fault
  is recorded at ≥2 window sizes (1-commit and 4-commit) with the culprit at
  varied non-head positions. Repeat runs of an identical config are robustness
  checks, not new cases (honest N).
- **Anti-leakage (enforced by `tests/test_corpus.py`):** the culprit is always a
  commit *contained in* the recorded window; for multi-commit windows it is
  **never** the release (window head) SHA. `release == culprit` is never a
  scoring path. Single-commit windows are the trivial case (release == culprit).
- **Abstention cases** (infra faults) carry a real *benign* release and no
  culprit — the correct answer is to decline to blame the innocent window.
- **Negative control** (`benign-deploy-baseline`) anchors the false-positive
  rate; without it the corpus is all-guilty.
- **Silent faults** (no Sentry event — N+1s, timeouts, silent regressions, the
  OOM infra fault) are **excluded from Sentry-driven top-k accuracy until M3's
  SNS ingest exists**. Their run records still carry culprit ground truth, so
  they join the eval then. Published N counts only scoreable cases, per class.

## Recording (Sentry + ngrok)

Sentry release association + webhook capture are gated on `SENTRY_DSN`, so the
pipeline runs fully in dry mode without them. To record the real corpus:

1. **ngrok**: `ngrok http 9000 --domain <static-domain>` → the recorder
   (`uv run culprit-harness record`). This domain is the Sentry webhook URL.
2. **Sentry** (free Developer): create a Django project → `SENTRY_DSN`; note
   `SENTRY_ORG` / `SENTRY_PROJECT` slugs (`SENTRY_URL=https://us.sentry.io` for
   the US region). Create ONE **internal integration** (Settings → Developer
   Settings → Custom Integrations) — it provides both the API token and the
   webhook secret:
   - Webhook URL = `https://<ngrok-domain>/sentry`, **Alert Rule Action ON**,
     Webhooks: **issue** checked.
   - Permissions: **Project: Read**, **Release: Admin**, **Issue & Event:
     Admin**, and **Organization: Read** (required — `set-commits` calls
     `GET /organizations/<org>/repos/`, which needs `org:read`).
   - Create a **Token** inside the integration → `SENTRY_AUTH_TOKEN`. Copy the
     **Client Secret** → HMAC verification.
   Add one issue-alert rule: *When* "A new issue is created" → *Then* "Send a
   notification via <integration>". Turn **Spike Protection off**.
   Put all of these in a gitignored `.env` and `source` it before running.
3. Rebuild the web image so `sentry-sdk` is installed. With `SENTRY_DSN` set,
   `run` does `sentry-cli releases new / set-commits --local --ignore-missing /
   finalize` (asserting ≥1 commit associated) and collects the `event_alert` +
   `issue` webhooks into `fixtures/sentry/{event_alert,issue}/`. The runner
   deletes the issue between runs so "A new issue is created" keeps firing.

   *Gotcha:* `set-commits` needs `--ignore-missing` — each scenario is a
   distinct fault branch, so a prior run's release SHA isn't in the next
   window's ancestry and `--local` would otherwise error.

**Quota budget**: Sentry free tier = 5k errors/month. Traffic is throttled
(`trigger.throttle_seconds`), `traces_sample_rate=0.0` (errors only), spike
protection OFF, `environment="fault-harness"`. The per-issue alert action
interval (~5 min) means one `event_alert` per issue per run — the runner
resolves/deletes the issue between runs and asserts webhook arrival.

## Deploy feed (GitHub `workflow_run`)

The GitHub half of the M2 ingest contract, mirroring the Sentry fixtures. Every
recorded run shipped a deploy — the release task (decision 7) that set the Sentry
release at `release_sha`. In production that deploy is theCourseForum2's **"AWS
Deployment"** GitHub Actions workflow, and GitHub emits a `workflow_run` webhook
describing it.

- **Fixtures:** `fixtures/github/workflow_run/<ts>-<hex>.json`, one per run, in the
  recorder envelope format. Each run record links its deploy via `deploy:`, and
  the deploy's `workflow_run.head_sha` **is** that run's `release_sha`.
- **Provenance:** reconstructed shape-faithfully from **real data** — the schema
  from a real upstream production "AWS Deployment" run, `head_sha`/`head_commit`/
  `repository`/`sender` from the real fork, only opaque ids/timestamps synthesized
  (deterministically). Full breakdown + field deltas in
  [`fixtures/github/workflow_run/PROVENANCE.md`](../fixtures/github/workflow_run/PROVENANCE.md).
- **Fidelity note:** `event == "workflow_run"` because the real "AWS Deployment"
  is *chained off CI* — a `push`-triggered fake would diverge on that field.
- **Anti-leakage:** `head_branch` is normalized to `master`, never the internal
  `fault/<id>-<ts>` branch — the deploy feed must not name the culprit
  (`tests/test_corpus.py::test_deploy_feed_never_names_the_culprit`).
- **Signatures:** signed with a **genuine** GitHub webhook secret
  (`CULPRIT_GH_WEBHOOK_SECRET`, in `.env`, never committed) — it was configured on a
  live `workflow_run` webhook on the fork and GitHub was confirmed delivering
  genuine events signed with it (temporary webhook since torn down), so
  `x-hub-signature-256` verifies as a real delivery would
  (`test_deploy_webhook_signatures_verify`, env-gated like the Sentry one).
- **Regenerate:** `set -a; source .env; set +a; culprit-harness backfill-deploys`
  (offline + deterministic; the secret makes the fixtures signed — omit it for
  unsigned).
- **To record live instead:** add [`harness/fork/fake-deploy.yml`](../harness/fork/fake-deploy.yml)
  to the fork's master, enable the fork's Actions, point a webhook at the recorder,
  and merge a window to master. `fake-deploy.yml` mirrors the real aws.yml trigger,
  so live payloads carry the same `event`/`head_branch`.

## Dump provenance

`db/local.dump` (gitignored) is a snapshot of the seeded harness DB at migration
`0028`, restored per scenario for a clean, resettable database the bad-migration
and infra faults can safely mutate. It is a **prod-copy with PII** — do not
share or commit it.
