# Culprit — Milestone 1 Executive Handoff

**Date:** 2026-07-04
**Status:** ✅ **COMPLETE.** Fault-injection harness built; 22-scenario labeled corpus recorded, validated, PII-scrubbed, and pushed.
**Branch:** `feat/m1-fault-injection-harness` → `github.com/IshanA2007/Culprit` (private), 20 commits.
**Prereq reading:** [`HANDOFF.md`](HANDOFF.md) (project vision), [`.claude/plans/culprit-m1-fault-injection-harness.plan.md`](.claude/plans/culprit-m1-fault-injection-harness.plan.md) (the M1 plan), [`docs/harness.md`](docs/harness.md) (runbook).

---

## 1. What M1 delivers (the TL;DR)

A production-faithful fork of **theCourseForum2** running locally under Docker + real Sentry, into which we inject **13 labeled faults** and record the resulting real webhook payloads as an eval corpus. That corpus is three things at once:

1. **The demo** — manufactured incidents on a fork of their real code (their real incident rate is too low to wait for).
2. **The eval source** — every future resume number (top-1/top-3 culprit accuracy, abstention rate, false-positive rate) is computed from these labeled runs.
3. **The Milestone-2 ingest contract** — the recorded JSON *is* the shape M2's `POST /ingest/*` service must parse.

**One-line pitch stays true:** we now have a rig that proves, with honest numbers, that Culprit can name the culprit commit for an injected production incident.

---

## 2. The corpus (the deliverable)

`runs/` (22 records) + `fixtures/` (committed, in the repo):

| | Count | Notes |
|---|---|---|
| Code faults | 9 types × 2 window sizes = **18 runs** | each fires its expected signal |
| Infra abstention faults | **3 runs** | redis-down, db-stopped, gunicorn-oom |
| Benign-deploy baseline | **1 run** | negative control (false-positive anchor) |
| Sentry webhooks | **26** | `fixtures/sentry/{event_alert,issue}/` — event_alert has stack frames, issue has counts |
| GitHub deploy webhooks | **22** | `fixtures/github/workflow_run/` — one `workflow_run` ("AWS Deployment") per run; `head_sha` == that run's `release_sha` (deploy-feed ingest contract) |
| Log captures | **22** | `fixtures/logs/` — gunicorn/docker stderr (silent-fault evidence) |

**Every run record** (`runs/<ts>-<fault>-w<n>.yaml`) is the answer key: base SHA, the ordered window commits (each flagged `is_culprit`/`is_decoy`), the `release_sha` (= window head), `ground_truth` (`culprit_commit` / `abstain` / `no_incident`), the decoy config, and paths to its fixtures + logs.

**Corpus invariants (enforced by `tests/test_corpus.py`, all green):**
- Culprit is always a commit **contained in** the recorded window, and for multi-commit windows is **never** the release SHA (the release is a decoy). No eval can score by echoing `release` or "newest commit."
- Abstention runs carry a real *benign* release and no culprit.
- Baseline has no culprit.
- Every recorded SHA is **resolvable on the fork** (M2 reads diffs/blame at these SHAs via the GitHub API — they must stay fetchable; the fork branches + tags are retained).
- Every webhook's HMAC signature verifies against the Sentry integration Client Secret.
- No orphaned fixtures (every fixture belongs to a run) — Sentry **and** GitHub deploy fixtures.
- Every run's deploy `workflow_run.head_sha` equals its `release_sha`, and the deploy feed's `head_branch` never names the culprit (anti-leakage extends to the deploy timeline).

---

## 3. How it works (architecture)

**Two repos, deliberately.**
- **Culprit** (this repo): the harness engine, fault definitions, recorder, corpus, tests.
- **The fork** (`IshanA2007/theCourseForum2`, branch `culprit-harness`): the run-profile changes + materialized fault windows. A gitignored full working clone lives at `.harness-work/theCourseForum2` (the harness thrashes it; never committed into Culprit).

**One scenario run (`culprit-harness run <fault>`), the decision-7 sequence** (`harness/scenarios/runner.py`):
1. reset the harness DB from `db/local.dump`
2. build a deploy window on a `fault/<id>-<ts>` branch — decoys interleaved with the fault commit, culprit forced to a **non-head** slot for multi-commit windows (anti-leakage)
3. tag + push the window to the fork (retained forever)
4. `sentry-cli` release at the window-head SHA + `set-commits --local --ignore-missing` + finalize
5. run the release task exactly as tCF's `aws.yml` does (`migrate && collectstatic && invalidate_cachalot && clearsessions`)
6. **recreate** the web container so the new `SENTRY_RELEASE` env bakes in; assert it reports the new release
7. provision an auth session + CSRF pair if the fault needs it
8. drive throttled trigger traffic (infra faults: run the docker action instead)
9. collect the Sentry webhooks (via the recorder) + capture container stderr logs
10. write the run record
11. cleanup — restore infra, reset the working branch, **delete the run's Sentry issues** (so the next run's "new issue created" alert fires)

`culprit-harness record-corpus` runs all 22 sequentially (they share Docker/DB — cannot be parallelized).

---

## 4. Live environment state (what's set up on this machine)

| Thing | State |
|---|---|
| tCF working clone | `.harness-work/theCourseForum2` (gitignored), branch `culprit-harness`, `origin`=fork, `github`=upstream. Has `.env` (from `.env.example`). |
| Harness Docker profile | `docker compose -f docker-compose.harness.yml` → `culprit_web` (gunicorn :80→host :8000), `culprit_db` (named volume `culprit_pgdata`), `culprit_redis`. **Currently up.** |
| Dev tCF container | `tcf_django` **stopped** (to free port 8000; its seed data is intact — never `docker rm` it). |
| DB dump | `db/local.dump` — gitignored, prod-copy PII, at migration **0028**. The per-scenario reset source. |
| `uv`, `sentry-cli`, `ngrok` | installed. |
| Recorder + ngrok tunnel | **torn down** after recording (the tunnel was a public endpoint — closed for hygiene). |

**Secrets live in `Culprit/.env` (gitignored — never commit):** `SENTRY_DSN`, `SENTRY_ORG=logan-bradley`, `SENTRY_PROJECT=culprit-tcf`, `SENTRY_URL=https://us.sentry.io`, `SENTRY_AUTH_TOKEN`, `SENTRY_CLIENT_SECRET`, `NGROK_DOMAIN=gush-limelight-thing.ngrok-free.dev`. Source it before running: `set -a; source .env; set +a`.

**Sentry setup** (org `logan-bradley`, project `culprit-tcf`): one **internal integration** `culprit-harness` with permissions **Org:Read + Project:Read + Release:Admin + Issue&Event:Admin**, webhook `https://gush-limelight-thing.ngrok-free.dev/sentry`, Alert Rule Action ON, issue webhook ON. One issue-alert rule "A new issue is created → notify culprit-harness". Spike protection OFF.

**To record again** (fresh session): bring up the harness profile, `culprit-harness record` (recorder on :9000) + `ngrok http 9000 --url gush-limelight-thing.ngrok-free.dev`, then `culprit-harness run …` or `record-corpus`. Always `scrub-fixtures` before committing.

---

## 5. Hard-won gotchas (these will bite whoever picks this up)

1. **`sentry-cli set-commits` needs `--ignore-missing` AND the token needs `org:read`.** Each scenario is a separate fault branch, so a prior run's release SHA isn't in the next window's git ancestry — `--local` errors without `--ignore-missing`. And set-commits first calls `GET /organizations/<org>/repos/` (needs `org:read`).
2. **A Sentry-visible fault records 0 fixtures if an identical-fingerprint issue already exists.** The "A new issue is created" alert only fires for *new* issues. The runner purges issues between runs; but ad-hoc `curl` diagnostics against the DSN-configured running web also create issues that will mask the next run — `purge_environment_issues()` first.
3. **Diagnostic curls against the running web create orphan fixtures** (real webhooks, no run record). `test_corpus.py` enforces none; scrub/clean before committing.
4. **Django `{% for x in qs %}` swallows query errors** (`ignore_failures=True`). A bad migration that only breaks a *template-loop* query won't 500. Target a column evaluated **view-side** (e.g. `Semester.latest()` in the context processor) instead. This is why the bad-migration fault drops `Semester.season`, not `Review.email`.
5. **No tCF page sets the `csrftoken` cookie on a GET**, so authenticated POST faults can't scrape one — `harness/auth.py` mints a valid CSRF cookie/header pair with `RequestFactory` + `get_token`.
6. **The seeded DB is behind the code** (dump was migration 0023; code is 0028) — the release task's `migrate` reconciles it. `local.dump` is re-snapshotted at 0028.
7. **The seed lives only in the dev `tcf_db` container's writable layer** (no persistent volume in the dev compose). Never `docker rm tcf_db` — `db/local.dump` is the only backup.
8. **The shell here is zsh** — it does not word-split unquoted variables; `set -- $var` loops silently do nothing.

---

## 6. PII posture

The DB is a prod copy: **21,591 real `@virginia.edu` student emails**, ~1,545 `pbkdf2` password **hashes** (not plaintext), reviews. Emails are **not** public (they de-anonymize reviews).
- `db/local.dump` is **gitignored** — the hashes/emails never leave the machine.
- Recorded fixtures captured student emails in stack-frame locals. `culprit-harness scrub-fixtures` (`harness/scrub.py`) redacts every email → `redacted@example.com` and **re-signs** the HMAC over the scrubbed body (so verification still holds); marks each `"scrubbed": true`. Verified **0 non-redacted emails** in the pushed tree.
- The repo is private. Owner decision on record: acceptable to commit scrubbed fixtures to the private repo.

---

## 7. Honest accounting — done vs. deferred

**Done:** all 10 plan tasks — scaffold, fork + seeded local boot, production-faithful Docker profile, Sentry wiring, webhook recorder, 13-fault catalog (9 code + 3 infra + baseline) as verified patches + manifest, scenario runner, the 22-scenario corpus, the fixture/corpus test suite, the runbook.

**Resolved after handoff (2026-07-04) — the `workflow_run` deferral is closed:**
- **GitHub `workflow_run` / deploy-feed fixtures now exist** (`fixtures/github/workflow_run/`, 22 — one per run). The recording session had no live tunnel, so rather than hand-invent payloads they are **reconstructed shape-faithfully from real data**: the field schema is a real upstream production "AWS Deployment" run (`harness/deployfeed_inputs/template_upstream.json`; key-parity enforced by `tests/test_deployfeed.py`), and `head_sha`/`head_commit`/`repository`/`sender` are the real fork objects — each run's deploy `head_sha` **is** its `release_sha`. Only opaque ids/timestamps are synthesized (deterministically). Generator: `harness/deployfeed.py` (`culprit-harness backfill-deploys`); each run record now carries a `deploy:` link; provenance + field deltas in `fixtures/github/workflow_run/PROVENANCE.md` and `docs/harness.md`. The plan's `fake-deploy.yml` ("AWS Deployment") also now exists at `harness/fork/fake-deploy.yml` (mirrors the real aws.yml's CI-chained trigger, so a **live** capture would carry the same `event: workflow_run` / `head_branch: master`). **Signatures:** a live `workflow_run` webhook was configured on the fork and GitHub was confirmed delivering genuine events to it (deliveries logged 2026-07-04T16:55Z, signed with `CULPRIT_GH_WEBHOOK_SECRET`); that temporary webhook + its `culprit-deploy-feed` branch were then torn down (fork left in its prior state). The committed fixtures are signed with that same genuine fork webhook secret (in `.env`, never committed), so `x-hub-signature-256` verifies exactly as a genuine delivery would — the payload *content* is reconstructed (stamped `reconstructed: true`), the *signature* is against a genuine GitHub webhook secret. To capture a raw GitHub-delivered payload verbatim later, re-establish a webhook, `gh auth refresh -s admin:repo_hook`, and read the delivery log (see PROVENANCE.md).

**Deferred / not done (be honest about these):**
- **Silent faults are excluded from Sentry-driven top-k accuracy** until M3's SNS/CloudWatch ingest exists (they emit no Sentry event by design; their run records carry culprit ground truth, so they join the eval then). This is intended, not a bug — but the published N must state it per class.
- **Sentry issue-API snapshots** (`count`/`userCount` as standalone secondary fixtures for M3 impact math) weren't captured separately — the `issue` webhook already carries `count`, which suffices for now.
- A few faults would be caught by tCF's own test suite (`passes_tcf_tests: false` in the manifest) — documented honestly as "requires a test gap to ship."

---

## 8. Path to Milestone 2 (the actual service)

The corpus in this repo is M2's input. Per [`HANDOFF.md`](HANDOFF.md) §4/§6, M2 is the core pipeline:

- **`fixtures/sentry/*.json` = the `POST /ingest/sentry` contract.** The recorder (`harness/recorder/app.py`) is the seed of the ingest service — it already writes the exact envelope M2 parses (`received_at`, `headers`, raw body). Reuse it.
- **`runs/*.yaml` = the eval cases** for M2/M5's top-1/top-3 numbers. "Top-3 of what?" is answered by each run's `window` (the recorded candidate commits). Score against `ground_truth` + `culprit_sha`.
- **First real M2 code:** Sentry webhook → signal/incident model (Postgres) → correlation window → evidence gathering (GitHub diffs/blame at the pinned window SHAs — they're resolvable on the fork) → culprit ranking (or abstain) → chat brief. Build the deploy-timeline table too — the `workflow_run` fixtures now exist (`fixtures/github/workflow_run/`; `POST /ingest/github` contract), each keyed by `head_sha` == a run's `release_sha`.
- **Mirror the toolchain** already established here: uv / ruff / pytest / py3.12, FastAPI, the `harness/manifest.py` + `harness/runrecord.py` contract style.

---

## 9. File map

```
harness/
  cli.py            culprit-harness CLI (faults, record, run, record-corpus, scrub-fixtures)
  manifest.py       fault schema + validation        (THE central contract)
  runrecord.py      eval run-record schema + leakage helpers
  fork.py           deploy-window builder (decoys + fault, non-head culprit) + tag/push
  decoys.py         benign decoy commit pool
  sentry_release.py sentry-cli wrapper + issue purge
  auth.py           session + CSRF minting (no Cognito)
  traffic.py        throttled httpx trigger driver
  recorder/app.py   FastAPI webhook recorder → raw fixtures  (M2 ingest seed)
  deployfeed.py     GitHub workflow_run deploy-feed generator (M2 deploy-ingest seed)
  deployfeed_inputs/ vendored REAL GitHub objects (upstream schema template + fork repo/owner/release commits)
  fork/fake-deploy.yml "AWS Deployment" workflow — add to fork master for LIVE deploy-feed recording
  scrub.py          fixture PII scrubber (redact emails + re-sign)
  scenarios/
    runner.py       one full scenario (decision-7 sequence)
    corpus.py       record all 22 sequentially
  faults/
    manifest.yaml   the 13-fault catalog (labels, triggers, windows, notes)
    *.patch         9 verified code-fault diffs
    decoys/         6 benign decoy patches + decoys.yaml
fixtures/           the recorded corpus (sentry/{event_alert,issue}/, github/workflow_run/, logs/)
runs/               22 labeled run records (the answer key; each links its deploy)
tests/              scaffold + manifest + corpus + recorder invariants (22 passing)
docs/harness.md     runbook
.harness-work/      gitignored full clone of theCourseForum2 (fork's culprit-harness branch)
db/local.dump       gitignored prod-copy DB snapshot (reset source)
.env                gitignored secrets (Sentry, ngrok)
```

---

*M1 built and recorded end-to-end against live Sentry + a real fork. The harness works; the numbers it produces will be defensible. Milestone 2 is the service that consumes this corpus.*
