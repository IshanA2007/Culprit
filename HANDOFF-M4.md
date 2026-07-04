# Culprit — Milestone 4 Executive Handoff

**Date:** 2026-07-04
**Status:** ✅ **COMPLETE.** Postmortem generator built test-first; deterministic completeness green; **both gated-live paths exercised** (real GitHub-App PR + real Discord read); opened as a PR.
**Branch:** `feat/m4-postmortem-generator` → PR into `main` (M3 already merged via PR #3). 12 commits (plan + 9 tasks + honesty/wiring follow-ups), roughly one per task.
**Prereq reading:** [`HANDOFF.md`](HANDOFF.md) (§4 postmortem design, §6 Phase 4), [`.claude/plans/culprit-m4-postmortem-generator.plan.md`](.claude/plans/culprit-m4-postmortem-generator.plan.md) (the M4 plan), [`HANDOFF-M3.md`](HANDOFF-M3.md) (the diagnosis layer this consumes), [`docs/postmortems.md`](docs/postmortems.md) + [`docs/github-app.md`](docs/github-app.md) (the M4 runbook + access ask).

---

## 1. What M4 delivers (the TL;DR)

M4 closes the loop: a **resolved** incident becomes a **postmortem Markdown PR** on
the fork. Culprit drafts the doc from the M3-persisted `incidents.diagnosis` + the
deploy/signal/evidence audit trail + the Discord chat thread, opens a branch + PR
via a GitHub App, and **stops** — a human reviews and merges. It never auto-merges,
never publishes unilaterally.

It is three things at once:

1. **The finished product loop** — brief → diagnosis → (resolve) → postmortem PR.
   Every resolved incident produces a complete, human-reviewable draft.
2. **A new, honest eval number** — `culprit eval` now reports **postmortem
   completeness 21/21** over the incident-producing corpus, deterministic and
   LLM-free, alongside the unchanged M3 culprit numbers.
3. **The single write layer** — one tightly-scoped GitHub App (branch + PR on the
   fork, no merge). Reads stay on the existing read-only path; **zero live AWS**.

---

## 2. The numbers (`culprit eval`, deterministic)

The M3 headline is **unchanged** (M4 adds no culprit numbers). New M4 sections:

| Metric | Result | N |
|---|---|---|
| **Postmortem completeness** (dry-run: timeline · culprit/abstain · impact+method · ≥1 hypothesis · fix-or-honest-absence) | **21/21 (100%)** | 21 |
| Fixing commit captured (code faults → rollback to `base_sha`) | **18/18** | 18 |
| Resolved via SNS `ALARM→OK` auto-detect (infra faults) | **3/3** | 3 |
| Live PR open to a sandbox branch (GitHub App) | **pass** (opened + cleaned up) | 1, gated |
| Narrative fidelity (LLM Summary adds no new SHA/number/section) | **pass** | gated |

**Read the N honestly.** The baseline (benign deploy) produces no incident, so it
correctly yields **no** postmortem — completeness N is 21, not 22. Code faults that
the frameless ranker *abstained* on still get a complete postmortem ("No code
culprit — looks infrastructural") with an honest impact line (a latency/canary
alarm carries no request count → "~0 measured failed requests, method stated").

**Full suite: 272 passed, 1 skipped** (the only skip is the pre-existing M1
SHA-resolvability test that needs a local clone; it skips in CI too).

---

## 3. How it works (architecture)

The M3 loop is unchanged; M4 adds resolution, assembly, and the write path. **No
`signals` schema change.** Additive only: `incidents.resolved_at/fixing_sha/
resolution_source` + a new `postmortems` table (one row per incident — the write is
idempotent). Migration `c3d4e5f6a7b8`, up/down clean.

```
RESOLUTION (one core, three triggers) → resolve_incident():
  · operator     POST /incidents/{id}/resolve · culprit resolve <id>
  · auto-detect  CloudWatch ALARM→OK on POST /ingest/sns
  · Discord      signed /resolve interaction → POST /discord/interactions (Ed25519)
        │  captures the fixing commit from the deploy feed (most recent post-open
        │  deploy) — or honest None (infra remediation; the fix-side abstention)
        ▼
ASSEMBLY (deterministic decides, LLM phrases) → culprit/postmortem.py:
  frontmatter · Summary (LLM prose only) · Impact (method stated) · Timeline
  (deploys/signals) · Root cause (ranked hypotheses / abstention) · Resolution
  (fixing commit or infra) · Suggested runbook (offer-only) · Discussion (the
  Discord thread, gated) · footer (Culprit drafts, humans merge)
        ▼
WRITE PATH (the ONE write permission) → culprit/github_app.py:
  App JWT (RS256, pyjwt) → installation token → create branch → PUT file
  → open PR.  NO merge call exists.  Gated/inert → dry-run by default.
```

**Load-bearing decisions (M4):**
- **Deterministic decides, LLM phrases — held.** The whole doc is rendered from
  persisted incident data; the LLM writes the Summary paragraph only (never a SHA,
  number, or section). The completeness eval is LLM-free and reproducible.
- **Offer-only / never merge.** `culprit/github_app.py` has *no* merge call at all
  (asserted by `tests/test_github_app.py`); the App scope is `contents` +
  `pull_requests` write on the fork only. One PR per incident (idempotent).
- **Anti-leakage re-asserted.** Assembly reads no ground-truth field (source-grep
  asserted); the synthesized fix-deploy/thread fixtures name no fault or culprit
  sha; `culprit/eval/score.py` stays the only ground-truth reader.
- **Dry-run first.** `culprit postmortem <id>` renders the Markdown + PR request
  without pushing; `--open` (with the App configured) opens the real PR. The eval
  scores the dry-run body, so no live write is needed per run — one gated live PR
  proves the real path.

---

## 4. Live environment state (what's set up on this machine)

| Thing | State |
|---|---|
| Service DB | unchanged from M3 (`pgvector/pgvector:pg17`); + M4 migration `c3d4e5f6a7b8` (resolution fields + `postmortems`). |
| `.env` additions | **GitHub App:** `GITHUB_APP_ID`, `GITHUB_APP_PRIVATE_KEY_PATH`, `GITHUB_APP_INSTALLATION_ID` (installed on the fork, contents+PR write, no merge). **Discord read:** `DISCORD_BOT_TOKEN`, `DISCORD_INCIDENT_CHANNEL_ID`. All optional/inert; absent → dry-run + thread omitted. |
| New dep | `pyjwt` (App JWT; in `uv.lock`). Discord verify/read reuse `cryptography` + `httpx` (no vendor SDK). |
| Both gated-live tests | **RAN GREEN** — a real PR opened to the fork and was closed + branch deleted; a real incident-channel read returned the normalized message shape. |

**To run the whole thing** (fresh session):
```bash
export PATH="$HOME/.local/bin:$PATH"
set -a; source .env; set +a
export GITHUB_TOKEN=$(gh auth token)
docker compose up -d db && uv run culprit migrate     # + c3d4e5f6a7b8
uv run pytest                                          # 272 pass, 1 M1-clone skip
uv run culprit eval                                    # M3 numbers + postmortem completeness 21/21
uv run culprit resolve <id> && uv run culprit postmortem <id>   # resolve + dry-run render
POSTMORTEM_DRY_RUN=false uv run culprit postmortem <id> --open  # open the real PR (App)
```

---

## 5. Hard-won gotchas (these will bite whoever picks this up)

1. **The corpus records no fixes**, so the eval models the fix as a **rollback
   re-deploying `base_sha`**. Because `ingest_github` deliberately does *not*
   advance `run_started_at` on conflict (it protects the window base),
   `postmortem_eval._apply_fix_deploy` sets the base_sha deploy's `run_started_at`
   to the fix time so `_capture_fix_commit` (most-recent-post-open deploy) finds
   it. Infra faults get no fix-deploy → `fixing_sha` None → "infra remediation".
2. **Discord resolution is via *interactions* (the `/resolve` slash command),
   Ed25519-verified — not reactions.** Webhooks/interactions are request/response;
   the ✅-reaction path needs a Gateway websocket and is documented as a live-only
   extension, not built into the deterministic eval. Same for "Sentry issue quiet
   post-deploy" (time-based, awkward to fixture).
3. **The GitHub App private key** is resolved from `GITHUB_APP_PRIVATE_KEY` *or*
   `..._PATH`; the gated live-PR test un-skips on either. The `.pem` is gitignored.
4. **The impact line always states a method — even for zero.** A latency/canary
   alarm has no request count; the postmortem says "~0 measured failed requests
   (method: no error events …)" rather than a bare "~0". This is what makes the
   silent-fault postmortems *complete*.
5. **Fix-deploy fixtures live in `fixtures/github/workflow_run/`** alongside the
   release deploys but ship `base_sha` (not `release_sha`); `run.fix_deploy` links
   them. Tests that iterate that directory must scope to `run.deploy` vs
   `run.fix_deploy` (see `tests/test_ingest_github.py::_fixtures`).
6. **All M3 gotchas still apply** (Voyage native key, pgvector collation on a
   reused volume, an abstention is never a top-k hit, SNS `text/plain`, `gh auth
   token` is the dev credential, serve binds :8010).

---

## 6. Honest accounting — done vs. deferred

**Done:** all 9 plan tasks — resolution core + three triggers, deterministic
assembly, Discord thread reader, LLM narrative phrasing, GitHub App write path,
synthesized rollback/thread fixtures, the completeness eval + gated live-PR +
narrative-fidelity, and the docs/access-pack. Every task's Validate passed before
the next; each is a commit. **Both gated-live validations RAN** (GitHub App, Discord)
— none skipped.

**Deferred / not done (be honest):**
- **The reaction + Sentry-quiet resolution paths are documented, not built** — they
  need a Gateway websocket / a time heuristic; the interaction + SNS-OK + operator
  paths are the deterministic, eval-scored triggers.
- **The fix commit is a modeled rollback**, not a recorded real fix (the corpus has
  none). It is honestly `base_sha` for code faults and `None` for infra — a real
  resolved incident's real fix deploy upgrades this in place.
- **Not deployed to production** — that (plus the pitch + the two instrumentation
  PRs) is Milestone 5.

---

## 7. Path to Milestone 5 (eval, harden, pitch)

M4 completes the product surface. Per [`HANDOFF.md`](HANDOFF.md) §6 Phase 5:

- **The eval report is the pitch's headline:** culprit top-1/top-3 (M3) + postmortem
  completeness (M4), all honestly denominated, all reproducible.
- **Three ready artifacts for the VP:** the read-only IAM ask (`docs/aws/`), the
  CloudWatch-alarms Terraform (`docs/aws/alarms-proposal.tf`), and the GitHub-App
  write ask (`docs/github-app.md`) — each the minimal grant, each hand-to-maintainer.
- **The demo is the full loop** on a fork of THEIR code: inject → brief with
  culprit + diagnosis + impact + runbook → resolve → postmortem PR.
- **Harden + deploy Mode B** (self-hosted, cross-account read-only creds), then pitch.

---

## 8. File map (what M4 added)

```
culprit/
  resolution.py          resolve_incident + fixing-commit capture (3 triggers converge)
  postmortem.py          deterministic assembly + draft/publish (idempotent)
  discord_read.py        ThreadReader: Fixture + Discord (read-scoped, gated)
  discord_verify.py      Ed25519 interaction verification
  github_app.py          GitHubAppWriter: JWT→token→branch+file+PR (NO merge)
  ingest/discord.py      /resolve interaction parse (PING/PONG)
  eval/postmortem_eval.py  dry-run completeness (N=21) + gated live-PR
  (updated) models (+resolution fields, +postmortems), app (+/incidents/{id}/resolve,
            +/discord/interactions), ingest/sns (ALARM→OK), llm (phrase_postmortem),
            cli (resolve/postmortem), config, eval/score+cli
migrations/versions/      c3d4e5f6a7b8 resolution fields + postmortems table
harness/
  discordfeed.py         rollback fix-deploys + Discord thread fixtures + backfill
  discord_inputs/        vendored Discord message schema
  (updated) cli.py (backfill-postmortem-inputs), runrecord.py (+fix_deploy,+thread)
fixtures/discord/         21 thread fixtures + PROVENANCE.md
fixtures/github/workflow_run/  18 rollback fix-deploy fixtures
docs/                    postmortems.md · github-app.md · pipeline.md (+M4 section)
tests/                   test_resolution/postmortem/postmortem_eval/github_app/
                         discord_verify/discord_read/ingest_discord/discordfeed +
                         extended corpus/ingest_sns/ingest_github/llm
```

---

*M4 built and verified against the live fork + GitHub App + Discord + Anthropic,
test-first, task-by-task. The service now produces the full incident loop through a
drafted postmortem PR — offer-only, humans-merge, anti-leakage-safe, and honestly
denominated. Milestone 5 is eval + harden + the pitch.*
