# Plan: Culprit Milestone 1 — Fault-Injection Harness

**Source PRD**: `.claude/prds/culprit.prd.md`
**Selected Milestone**: 1 — Fault-injection harness
**Complexity**: Large

## Summary
Build the harness before the service: fork theCourseForum2, run it locally in a production-faithful Docker profile, wire Sentry (free tier) with release↔commit association, implement ~12 injectable faults with labeled ground truth (culprit commit **contained in** a multi-commit deploy window for code faults; "abstain — infrastructural" for infra faults; one benign-deploy negative control), and record real Sentry/GitHub webhook payloads as pytest fixtures. This corpus is the demo, the eval source, and the ingest contract for Milestone 2's pipeline.

All repo facts verified against a clone of `thecourseforum/theCourseForum2@dev` (2026-07-03) by a 4-agent recon workflow; Sentry facts against current docs. The plan then survived a 3-lens adversarial review (fidelity / executability / eval-integrity, 20 findings) — the eval-integrity fixes below (window design, release semantics, negative control) exist because of it.

## Patterns to Mirror
The Culprit repo is greenfield — **no internal patterns exist**. Per handoff §5, we deliberately mirror theCourseForum2's toolchain (refs are into their repo):

| Category | Source (tCF repo) | Pattern |
|---|---|---|
| Package mgmt | `pyproject.toml:5-39` | uv-managed, PEP 621 deps with bounded ranges (`>=x.y.z,<N`), dev tools in PEP 735 `[dependency-groups]` |
| Lint | `pyproject.toml:41-57` | ruff, target py312, line-length 88, `check` + `format --check` both CI-enforced |
| CI | `.github/workflows/ci.yml:23-41` | `astral-sh/setup-uv@v5`, Python 3.12, `uv sync --frozen --group dev` |
| Python | `pyproject.toml:4` | Strictly 3.12 (`>=3.12,<3.13`) |
| Tests (Culprit) | — none in tCF (they use Django unittest, no pytest) | **New convention**: pytest for Culprit's own code, per handoff §5. Fault commits pushed to the fork must remain compatible with their `manage.py test` runner |

## Architecture decisions (this milestone)

1. **Two repos.** Culprit (this repo) holds the harness engine, fault definitions, recorder, fixtures, tests. The fork (`<ishan>/theCourseForum2`) holds a long-lived `culprit-harness` branch with the run-profile changes (compose file, settings module, Sentry SDK). Fault commits are materialized on the fork at run time, branched off `culprit-harness`. The fork's `aws.yml` is disabled on this branch (no AWS secrets exist, and its "AWS Deployment" name must not collide with our deploy-feed workflow).
2. **Faults live in Culprit as patch files + a manifest**, not as pre-made fork branches. The scenario runner builds a **deploy window**: it interleaves the fault commit with plausible benign **decoy commits** (touching real `tcf_website` files, realistic messages — not README/whitespace edits), with the culprit's position randomized so the culprit is *not* reliably the window head. Ground truth = the fault commit SHA **within** the recorded window (base SHA + ordered commit list). Materialized refs are **tagged and retained on the fork** — Milestone 2 reads diffs/blame at pinned SHAs via the GitHub API, so recorded SHAs must stay resolvable.
3. **Production-faithful run profile, standalone compose file.** tCF's dev compose runs `runserver` with `DEBUG=True`, cachalot disabled, no Redis (`docker-compose.yml:7-15`, `tcf_core/settings/dev.py:26-39`) — several faults are invisible under that profile. `docker-compose.harness.yml` is **standalone** (not an overlay: compose concatenates `ports` across `-f` files, so overlaying the base's `8000:8000` would double-bind host 8000 and fail). It defines: web with explicit `command: ["bash", "scripts/container-startup.sh"]` (gunicorn :80, 3 workers × 2 threads, 120s timeout — as prod), published `8000:80`; `DJANGO_SETTINGS_MODULE=tcf_core.settings.harness` (`DEBUG=False`, Redis cache + `cached_db` sessions mirroring `prod.py:50-66`, `CACHALOT_ENABLED=True`, Sentry init); a `redis:7` service `tcf_redis`; db with healthcheck re-enabled; `SENTRY_*` env passthrough; overridable memory limits. **Note gunicorn startup runs no migrations** (`container-startup.sh` is exec-gunicorn only — same as prod, where migrate runs in a one-off ECS release task): the runner must run the release task explicitly (decision 7).
4. **Sentry capture = internal integration webhooks, both types.** `event_alert` (stack frames, release, triggered rule — no counts) and `issue` (count/userCount, firstSeen — no frames). `error.created` is Business-plan-only — ruled out. Issue-alert actions are rate-limited per issue (~5-min minimum interval), so expect **one `event_alert` per issue per run**, not per event; the runner resolves/deletes the issue between runs and asserts webhook arrival, failing loudly. Spike protection OFF; `traces_sample_rate=0.0` (errors-only, 5k events/month free quota).
5. **Release semantics designed against label leakage.** `SENTRY_RELEASE` = the **deploy-window head SHA** — which, by decision 2, is frequently a decoy commit, so `release` never functions as the answer key. Per window: `sentry-cli releases new $HEAD && set-commits --local && finalize` (requires a **full, non-shallow** fork clone; runner asserts ≥1 commit with non-empty patch_set was associated). Eval invariant: culprit ∈ release commit range — **never** release == culprit. **Infra faults run on top of a recent finalized benign release** (decoy-only window, deployed normally, then the docker action fires): events still carry a release and the deploy feed shows a recent innocent deploy, so abstention means *declining to blame benign commits*, not noticing a missing field.
6. **Recorder is a tiny FastAPI app** (Culprit repo) that dumps every request raw — body + headers (`Sentry-Hook-Resource`, `Sentry-Hook-Signature`, timestamp) — into `fixtures/` as-received; it grows into the Milestone 2 ingest service, so fixture format = ingest contract. Delivery via ngrok free static domain; cloudflared/webhook.site fallback.
7. **Deploys are simulated faithfully.** Per scenario the runner: pushes the window, runs the **release task** exactly as tCF's `aws.yml` does (`docker compose run --rm web bash -c "python manage.py migrate && python manage.py collectstatic --noinput && python manage.py invalidate_cachalot tcf_website && python manage.py clearsessions"`), then **recreates** the web container (`docker compose up -d web` — a plain `restart` would keep the old `SENTRY_RELEASE` env baked in at create time) and asserts the app now reports the new release before driving traffic. This also makes bad-migration faults actually apply, and reproduces prod's real migrate-applied-but-deploy-halted skew window.

## Files to Change

| File | Action | Why |
|---|---|---|
| `pyproject.toml` | CREATE | uv project: ruff (mirror tCF config), pytest, httpx, fastapi, uvicorn, pyyaml; dev group |
| `.gitignore`, `README.md` | CREATE | Standard scaffolding; README = harness quickstart |
| `.github/workflows/ci.yml` | CREATE | ruff check + format + pytest, uv-based, mirroring tCF's CI shape |
| `harness/__init__.py` | CREATE | Package root |
| `harness/cli.py` | CREATE | `culprit-harness` CLI: `up`, `seed`, `snapshot`, `reset`, `run <fault-id>`, `revert`, `record` |
| `harness/fork.py` | CREATE | Fork checkout mgmt: build deploy window (fault + decoys, randomized position), tag + push refs, capture SHAs, reset to `culprit-harness` |
| `harness/decoys.py` | CREATE | Pool of plausible benign app-code commits (real `tcf_website` file edits) with realistic messages |
| `harness/sentry_release.py` | CREATE | Wraps sentry-cli: release new/set-commits --local/finalize; asserts commits+patch_set associated |
| `harness/auth.py` | CREATE | Authenticated-session provisioning without Cognito (`manage.py shell` user+session; sessionid cookie + CSRF token for traffic driver) |
| `harness/traffic.py` | CREATE | httpx traffic driver: per-fault trigger requests, throttled (quota), auth-aware (`requires_auth` faults) |
| `harness/recorder/app.py` | CREATE | FastAPI catch-all recorder → raw JSON fixtures with headers + received_at |
| `harness/faults/<id>.patch` (~12) | CREATE | The fault diffs (catalog below) |
| `harness/faults/manifest.yaml` | CREATE | Per fault: category, class, commit message, trigger requests, `requires_auth`, expected symptom, expected signal, ground-truth label, window/decoy spec, `passes_tcf_tests` |
| `harness/scenarios/runner.py` | CREATE | Orchestrates the full run (decision 7 sequence) |
| `fixtures/sentry/{event_alert,issue}/…json` | CREATE (recorded) | The corpus — raw, labeled via sidecar `…meta.yaml` |
| `fixtures/logs/…` | CREATE (recorded) | gunicorn/docker stderr captures for silent faults (WORKER TIMEOUT, OOM SIGKILL lines) |
| `fixtures/github/workflow_run/…json` | CREATE (recorded) | Deploy-feed payloads from fork Actions (recorded off the fork's master so `head_branch`/`event` match prod values) |
| `runs/<ts>-<id>.yaml` | CREATE (recorded) | Run records: base SHA, ordered window commits (sha, message, is_culprit), decoy config, timestamps, fixture paths, ground truth |
| `tests/test_fixtures.py`, `tests/test_manifest.py`, `tests/test_corpus.py` | CREATE | Corpus invariants (Task 9) |
| `docs/harness.md` | CREATE | End-to-end demo runbook (doubles as pitch-demo script); documents eval-denominator rules |
| **Fork**, branch `culprit-harness`: `docker-compose.harness.yml`, `tcf_core/settings/harness.py`, `pyproject.toml` (+`sentry-sdk[django]`), `.github/workflows/fake-deploy.yml`, aws.yml disabled | CREATE/UPDATE | Run profile + Sentry wiring + deploy-feed workflow named "AWS Deployment" (collision-free once the original is disabled) |

## Fault catalog v1 (recon-verified injection points)

Code faults — ground truth = culprit commit within window (9):
| id | category (handoff) | Sentry sees it? | notes |
|---|---|---|---|
| `bad-migration-drop-review-email-column` | bad migration | yes — `ProgrammingError` 500s on course-instructor pages | needs release task (decision 7) |
| `template-noreversematch-instructor-card` | template crash | yes — `NoReverseMatch` on all course pages | |
| `search-fielderror-500` | code crash | yes — `FieldError` on every search + autocomplete keystroke | tCF's own test suite would catch this (`test_search.py:129-169`) — manifest marks `passes_tcf_tests: false`; documented as "requires a test gap to ship" for honest eval framing |
| `vote-duplicate-integrityerror` | endpoint 500 | yes — intermittent, user-state-dependent | `requires_auth: true` — vote endpoints are `@login_required` (`votes.py:22,30,39`); needs `harness/auth.py` sessions + pre-seeded opposite-vote state |
| `landing-import-crash-bad-deploy` | bad deploy | yes — import-time crash; health check still passes (looks infra-y) | |
| `n-plus-one-section-instructor-prefetch` | N+1 latency | **no** — extra queries, latency only | promoted from stretch (scoreable-case count, see Task 8) |
| `cartesian-join-gpa-annotation-timeout` | N+1/timeout | **no** — worker-timeout 502/504s | silent-fault signal story below |
| `search-silent-zero-results` | silent regression | **no** — zero errors, behavior change only | tests abstention from error-reasoning |
| `bad-migration-drop-trigram-gin-indexes` | bad migration (perf) | **no** — search latency 10-100x | promoted from stretch |

Stretch: `cachalot-missing-invalidation`, `gunicorn-entrypoint-typo-bad-deploy`, `auth-cognito-audience-break`, `grade-load-wipe-then-crash`.

Infra faults — ground truth = **abstain**, run on top of a fresh benign release (3):
| id | action | signal signature |
|---|---|---|
| `redis-down` | `docker stop tcf_redis` | ConnectionError flood, all transactions, recent deploy is innocent |
| `db-stopped` | `docker stop/pause tcf_db` | OperationalError storm (stop) or silent 504 hang (pause) |
| `gunicorn-worker-oom` | `docker update --memory=256m` + heavy traffic | sporadic 502s, SIGKILL, **zero Sentry events** |

Negative control (1): `benign-deploy-baseline` — decoy-only window, deployed normally, normal traffic, no fault. Ground truth = "no incident / no culprit". Without it, M5 cannot report a false-positive rate and the corpus contains only guilty cases.

**Silent-fault signal story (M1 scope):** silent faults emit no Sentry webhook and the local harness has no ALB/CloudWatch. M1 records what *does* exist — gunicorn/docker stderr log captures (`fixtures/logs/`) — and documents the eval-denominator rule: silent faults are excluded from Sentry-driven top-k accuracy until M3's SNS ingest exists (their run records already carry culprit ground truth, so they join the eval then; M3 will spec synthetic-but-shape-faithful SNS payloads against real SNS documentation). Published N counts only scoreable cases, stated per class.

## Tasks

### Task 1: Scaffold the Culprit repo
- **Action**: `pyproject.toml` (uv, py312, ruff config copied from tCF's `pyproject.toml:41-57` minus Django-specific rules; deps: fastapi, uvicorn, httpx, pyyaml; pytest in dev group), `.gitignore`, README stub, CI workflow. First commits: HANDOFF.md, PRD, this plan, scaffolding.
- **Mirror**: tCF `pyproject.toml` + `ci.yml` shapes.
- **Validate**: `uv sync && uv run ruff check . && uv run pytest --co`; CI green on push.

### Task 2: Fork + verified local boot
- **Action**: Fork theCourseForum2 under Ishan's GitHub. **Full clone — never shallow** (`sentry-cli releases set-commits --local` must walk history; `git fetch --unshallow` if inherited shallow). `cp .env.example .env` (shipped values suffice — verified); seed DB via the **public** gdown file `1TsSvhvWGA24537xNo_9CkULKzjugNrZH` → `db/latest.sql` (147 MB, anonymously downloadable — verified; the documented Drive folder is login-gated) + `psql` import with `pg_trgm` (their `setup.sh:63-66` steps run manually — their `reset-db.sh` uses the legacy `docker-compose` binary). **Immediately snapshot the dump to our own storage.** Then `scripts/local_dump.sh` → `db/local.dump` for fast per-scenario resets. `docker compose up` → site on :8000 with real data.
- **Mirror**: their documented flow `doc/dev.md:14-39`.
- **Validate**: `curl -fs localhost:8000/health` returns "ok"; a course page renders with GPA data. (First-boot web↔db race is benign — their healthcheck is commented out; don't mistake restart noise for injected faults.)

### Task 3: Production-faithful harness profile on the fork
- **Action**: Branch `culprit-harness`. Add standalone `docker-compose.harness.yml` per decision 3 (explicit `command`, `8000:80`, harness settings, `tcf_redis`, db healthcheck, `SENTRY_*` passthrough, mem limits). Add `tcf_core/settings/harness.py` (base + `DEBUG=False` + `ALLOWED_HOSTS=["*"]` + Redis CACHES/`cached_db` sessions per `prod.py:50-66` + `CACHALOT_ENABLED=True`). Disable `aws.yml` on this branch.
- **Critical check**: read `tcf_core/settings/handle_exceptions_middleware.py` and confirm it re-raises after printing JSON — if it swallows exceptions, Sentry never sees faults; adjust **in harness settings only** (finding feeds the tCF instrumentation-PR pitch asset).
- **Validate**: `docker compose -f docker-compose.harness.yml up -d`; gunicorn serves on :8000 (container :80); release task command (decision 7) runs clean; a deliberate scratch-view `raise` produces the JSON stderr line; cachalot keys visible in `redis-cli`.

### Task 4: Sentry wiring + webhook plumbing
- **Action**: On `culprit-harness`: `uv add 'sentry-sdk[django]'`; init in `harness.py` (`DjangoIntegration`, `environment="fault-harness"`, `release=env("SENTRY_RELEASE")`, `traces_sample_rate=0.0`, `send_default_pii=True`). Sentry side (free Developer plan): new project; **internal integration** ("Alert Rule Action", Issue&Event:Read, webhook URL = ngrok static domain, Issue-webhook checkbox ON); one issue alert rule routed to the integration. Record the Client Secret for HMAC verification. Accept the per-issue action-interval reality (decision 4): the runner resolves/deletes the fault's issue between runs and spaces repeats.
- **Mirror**: recon-verified snippet; their `django-environ` `env()` idiom.
- **Validate**: scratch exception → Sentry event with correct release + environment → `event_alert` AND `issue` webhooks arrive at the recorder with frames/release present.

### Task 5: Webhook recorder
- **Action**: `harness/recorder/app.py` — FastAPI catch-all `POST /{source:path}` writing `{received_at, path, headers, raw_body}` to `fixtures/<source>/<ts>-<resource>.json` untouched; responds 200 in <1s (Sentry's timeout). GitHub `workflow_run` route included.
- **Mirror**: none (greenfield); seeds Milestone 2's ingest app.
- **Validate**: `uv run pytest tests/test_recorder.py` (raw body byte-identical); live Sentry test webhook lands via tunnel.

### Task 6: Fault catalog as patches + manifest
- **Action**: Encode 9 code + 3 infra + 1 baseline as `harness/faults/<id>.patch` (or docker actions) + `manifest.yaml` per the schema above, including `requires_auth`, window/decoy spec, and `passes_tcf_tests` (measured by running `manage.py test` on the patched tree — faults their CI would catch are kept but explicitly documented as "requires a test gap to ship").
- **Mirror**: patches pass **local** `ruff check` + `djlint` on the patched tree (tCF's CI does not run on fork fault-branches — its triggers are `pull_request` and push to `dev`/`master` only — so lint-cleanliness is our own gate, not theirs).
- **Validate**: `tests/test_manifest.py` — every patch applies cleanly to the pinned base SHA and reverts; schema validates; all 5 handoff categories covered; ≥3 abstention, ≥2 silent, 1 baseline present.

### Task 7: Scenario runner
- **Action**: `harness/scenarios/runner.py` + CLI implementing decision 7:
  (1) reset DB from `local.dump`; (2) build deploy window on `fault/<id>-<ts>`: decoys + fault commit at randomized position (baseline/infra scenarios: decoys only); (3) tag + push refs to fork (**retained** — never deleted); (4) `sentry-cli` release at window-head SHA, `set-commits --local`, finalize; assert ≥1 commit with patch_set; (5) release task (`migrate && collectstatic && invalidate_cachalot && clearsessions` via `compose run --rm web`); (6) **recreate** web (`up -d web`) with `SENTRY_RELEASE=<window head>`; assert app reports the new release before proceeding; (7) provision auth session if `requires_auth` (via `harness/auth.py`; CSRF-aware); (8) drive throttled trigger traffic; infra faults: execute docker action instead of 2–6's fault commit (their benign window was deployed first); (9) collect webhooks + capture container stderr logs; (10) write run record (base SHA, ordered window commits with `is_culprit`, decoy config, injection/first-signal timestamps, fixture paths, ground truth); (11) cleanup: reset working branch, restore memory limits/containers, resolve/delete the Sentry issue — refs and tags stay.
- **Mirror**: deterministic code for anything countable (handoff §3 consequence).
- **Validate**: `uv run culprit-harness run template-noreversematch-instructor-card` → labeled fixtures whose event release == window head ≠ culprit (when decoys follow the fault); `run redis-down` → abstention-labeled corpus where events still carry the benign release; `run benign-deploy-baseline` → run record with no-incident ground truth and no alert webhook.
- **Note**: an eval consumer must never be able to score by echoing `release` or "newest commit" — that property is Task 9's job to enforce.

### Task 8: Record the corpus
- **Action**: Record every scenario with **varied window configs — each code fault at ≥2 window sizes** (single-commit and 3–5-commit) **with culprit position varied**; each (fault × window-config) pair is one eval case, so ≥18 culprit-scoreable cases from 9 code faults — repeat-runs of an identical config are robustness checks, not new cases (honest N, per handoff §8). Deploy-feed fixtures: merge windows to the **fork's master** so `fake-deploy.yml` (named "AWS Deployment") fires with `head_branch: master` matching real prod payload values (recon-verified: real deploy events have `head_branch=master`, `event=workflow_run`); document any residual field deltas in the fixture sidecar. Snapshot Sentry issue-API responses (`count`, `userCount`) as secondary fixtures for M3 impact math.
- **Validate**: fixture inventory — every manifest fault has run records at required window configs; expected signal class present (or expected-absent + log capture for silent faults); raw headers preserved.

### Task 9: Fixture test suite + CI
- **Action**: pytest invariants over the corpus:
  - every `event_alert` fixture parses; `data.event.exception.values[].stacktrace.frames[]` and `data.event.release` present;
  - **anti-leakage**: every code-fault run's culprit SHA is **contained in** its release's recorded commit window; in ≥⅓ of multi-commit cases the culprit is *not* the window head; release == culprit-SHA equality never asserted anywhere;
  - abstention runs carry the benign release (field present) and no fault commit in their window;
  - baseline run has no culprit label and no alert fixtures;
  - silent-fault runs: no Sentry fixtures, log capture present, run record carries culprit ground truth;
  - every recorded SHA (window commits, tags) is **fetchable from the fork remote** (corpus doesn't dangle);
  - `Sentry-Hook-Signature` verifiable with the env-injected secret (never committed).
- **Validate**: `uv run pytest` green locally + CI.

### Task 10: Harness runbook
- **Action**: `docs/harness.md`: one-command demo path (boot → run one code fault → show Sentry issue + captured payloads → revert), fault-catalog table, **eval-denominator rules** (what counts as a case; silent-fault deferral to M3), quota budget, tunnel setup, dump provenance. Doubles as the pitch-demo script (handoff §6).
- **Validate**: cold-start dry run following only the doc.

## Validation
```bash
# Culprit repo
uv sync && uv run ruff check . && uv run ruff format --check . && uv run pytest
# Fork harness profile (standalone compose file)
docker compose -f docker-compose.harness.yml up -d && curl -fs localhost:8000/health
# End-to-end scenarios (the milestone's definition of done)
uv run culprit-harness run template-noreversematch-instructor-card   # code fault, multi-commit window
uv run culprit-harness run redis-down                                # abstention on top of benign release
uv run culprit-harness run benign-deploy-baseline                    # negative control
ls fixtures/sentry/event_alert/ fixtures/sentry/issue/ fixtures/logs/ runs/
```

## Risks
| Risk | Likelihood | Mitigation |
|---|---|---|
| Public DB dump goes stale/private (documented path is login-gated) | Medium | Verified-public gdown ID works today; snapshot to own storage in Task 2, never re-depend on Drive |
| `HandleExceptionsMiddleware` swallows exceptions → Sentry blind | Medium | Explicit Task 3 check before fault work; harness-settings-only adjustment; informs tCF instrumentation PR |
| Sentry 5k errors/month quota exhausted | Medium | Throttled traffic driver; spike protection off; errors-only; `fault-harness` environment scoping |
| Per-issue alert action interval (~5 min) suppresses repeat webhooks | High (certain) | Designed-in (decision 4): resolve/delete issue between runs, space repeats, runner asserts arrival and fails loudly |
| Auth-session provisioning without Cognito proves fiddly | Medium | `harness/auth.py` is scoped to shell-created sessions + CSRF; only one core fault depends on it — demote `vote-duplicate-integrityerror` to stretch if it stalls >½ day |
| Tunnel flakiness during recording | Low | ngrok static domain primary; webhook.site one-shot capture fallback; recorder responds <1s |
| Fault patches rot as tCF's dev branch moves | Medium | Pin `culprit-harness` to a recorded base SHA; manifest test re-validates patch application in CI |
| Eval numbers challenged as rigged (handoff §8) | Medium | Anti-leakage invariants (Task 9), randomized culprit position, plausible decoys, negative control, honest per-class denominators — all corpus-level and testable |

## Forward visibility → Milestone 2
- Fixture JSON (raw body + headers) **is** the `POST /ingest/sentry` contract; the recorder grows into the ingest service.
- Run records with window commit lists are the eval cases for M5's top-1/top-3 numbers ("top-3 of what" = the recorded candidate window).
- `workflow_run` fixtures define the deploy-timeline schema (`head_sha`, `run_started_at`, `updated_at`, `conclusion`, plus the CI-skipped-job guard recon identified).
- Sentry issue-API snapshots seed the impact calculator; `fixtures/logs/` previews the M3 CloudWatch/SNS path for silent faults.

## Acceptance
- [ ] All tasks complete; validation commands pass (one code-fault, one infra-fault, and the baseline scenario end-to-end)
- [ ] ≥12 scenarios: all 5 handoff categories, ≥3 abstention-class, ≥2 silent, 1 benign-deploy negative control
- [ ] Every code fault recorded at ≥2 window sizes with varied culprit position; decoys are plausible app-code commits, recorded in run records
- [ ] Anti-leakage invariants green: culprit ∈ window (never == release), abstention cases carry releases, all recorded SHAs resolvable on the fork
- [ ] Eval-denominator rules documented (silent-fault deferral to M3 stated explicitly)
- [ ] Patterns mirrored from tCF toolchain, not reinvented
