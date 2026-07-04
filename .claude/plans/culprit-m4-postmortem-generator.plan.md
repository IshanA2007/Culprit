# Plan: Culprit Milestone 4 — Postmortem Generator

**Source PRD**: `.claude/prds/culprit.prd.md`
**Selected Milestone**: 4 — Postmortem generator
**Complexity**: Large

## Summary
Turn a **resolved** incident into a **postmortem Markdown PR** on the fork. M4 adds three layers on top of M3's persisted `incidents.diagnosis`: (1) **resolution detection** — the trigger — as one deterministic core (`resolve_incident`) reached by three converging paths: an operator/eval path (`POST /incidents/{id}/resolve` + `culprit resolve`), an **auto-detect** path (a CloudWatch alarm's `ALARM → OK` transition on the existing `/ingest/sns`), and the **Discord-native** path (a signed `/resolve` slash-command interaction). Resolution captures the **fixing commit from the deploy feed** (the most recent deploy after the incident opened — or honestly *none*, an infra remediation, the fix-side parallel to culprit abstention). (2) **Deterministic postmortem assembly** (`culprit/postmortem.py`) builds the doc from `incidents.diagnosis` (hypotheses + offered runbook + impact snapshot) + the `deploys`/`signals`/`evidence`/`jobs` audit trail (the timeline) + the measured impact + the fixing commit + the Discord chat thread (read via a gated `DiscordThreadReader`); the LLM phrases the narrative prose **only** — every fact and the whole structure are deterministic. (3) **The write path** (`culprit/github_app.py`): a GitHub App — **the single new write permission** (contents + pull-requests, on the fork) — creates a branch, writes `postmortems/YYYY-MM-DD-slug.md`, and opens a PR. **Culprit drafts; humans merge — it never auto-merges, never publishes unilaterally.**

**The gating risk is the same offer-only / no-live-write posture that defines the project**, so the milestone is sequenced to prove everything **without a live write on every run.** Tasks 1–5 (resolution, assembly, thread-read, LLM phrasing) touch no external write and run entirely on persisted data + shape-faithful fixtures; the postmortem eval runs in a **dry-run mode** (render the Markdown + the PR request without pushing) that is deterministic, reproducible, and LLM-free for the completeness metric; the **live PR** is a single gated test against a sandbox branch of the fork. **Zero live AWS anywhere** (M4 is a GitHub/Discord layer); reads stay on the existing read-only path and the GitHub App write is scoped to exactly branch-create + file + PR on the one fork.

## Patterns to Mirror
M1/M2/M3 established Culprit's conventions; M4 extends them. tCF/GitHub/Discord refs are for wire-shape only.

| Category | Source (`file:line`) | Pattern |
|---|---|---|
| Synthesized-fixture provenance | `harness/deployfeed.py:1-47`, `fixtures/github/workflow_run/PROVENANCE.md`, `harness/snsfeed.py` | Real field schema from a vendored real object; real substantive fields; only opaque ids/timestamps synthesized (deterministically, byte-stable); `"reconstructed": true` stamp; provenance doc; key-parity test — the Discord-thread + fix-deploy fixtures use this exact playbook |
| Backfill CLI | `harness/cli.py:120-190` (`backfill-sns`/`backfill-deploys`) | A `culprit-harness backfill-*` subcommand generates fixtures from the run records; run records gain a link field (`deploy:`, `sns:` precedent) |
| Provider-behind-interface + fixture impl | `culprit/cloudwatch.py:23-46` (`LogsProvider` Protocol; `FixtureLogsProvider`) | `enabled`-gated Protocol with a live impl + an offline `Fixture*` impl reading `fixtures/`; the Discord reader mirrors this exactly |
| Verify-then-parse ingest boundary | `culprit/app.py:126-170` (`/ingest/sns`), `culprit/sns_verify.py:43-120` | Dispatch on header, verify signature over the raw body (SNS X.509 → Discord Ed25519), 401 on bad/missing signature, SSRF host allowlist on any outbound fetch, idempotent on a unique key |
| Read-only GitHub client (the write path's twin) | `culprit/github_api.py:1-11,52-108` | httpx REST+GraphQL, disk cache, immutable-SHA reads; the **write** client is a *separate* module — the read path is never given write scope |
| Env-gated integration, inert when keyless | `culprit/config.py:26-71` | Every secret optional; absent secret → integration inert (+ its tests skip); the deterministic pipeline/eval runs with none; new App/bot creds follow suit |
| Deterministic-decides / LLM-phrases | `culprit/diagnosis.py:1-9`, `culprit/llm.py:123-152` (`phrase_diagnosis`) | The LLM writes a readable sentence over already-decided facts; it never introduces a fact or picks an answer; the eval never scores LLM output — `phrase_postmortem` is the same shape |
| Persisted diagnosis = M4 input | `culprit/pipeline.py:305-315`, `culprit/diagnosis.py:64-80`, `culprit/impact.py:62-72` | `incidents.diagnosis` already holds hypotheses + `runbook_id` + the impact snapshot; the postmortem reads *this*, never re-derives from ground truth |
| Additive schema, no `signals` change | `culprit/models.py:70-77` (`diagnosis`/`embedding` added additively) | New state is additive columns + a new sibling table (`postmortems`, like `evidence`/`jobs`); `signals` is untouched |
| Idempotent-on-unique-key | `culprit/models.py:97`, `culprit/correlation.py:82-83` | Exactly one brief per outage → exactly one postmortem PR per incident (unique `incident_id`); re-resolve never opens a second PR |
| Eval anti-leakage | `culprit/eval/replay.py:1-11`, `culprit/eval/score.py:1-9` | Pipeline/assembly see only ingest contract + deploy feed + persisted incident rows; `score.py` is the ONLY ground-truth reader |
| Honest per-class N + gated sections | `culprit/eval/score.py:143-202`, `culprit/eval/driver.py:36-62` | Each metric reports its N; LLM/network-dependent sections gated behind their key, own N, never pollute the deterministic headline |
| CLI argparse style | `culprit/cli.py:102-127` | `serve`/`migrate`/`eval` subparsers; `resolve`/`postmortem` mirror them |
| Resolve affordance (the trigger's UI) | `culprit/brief.py:106,137-138`, `culprit/models.py:59` (`status: open|resolved`) | The brief already renders "Resolve: react ✅ or reply `resolve`" and `status="resolved"`; M4 makes that affordance real |
| Access-pack docs | `docs/aws/aws-access.md`, `docs/aws/culprit-readonly-policy.json` | The GitHub-App scope ask is documented the same way the AWS read-only ask was — a concrete artifact to hand a maintainer |

## Architecture decisions (this milestone)

1. **M4 extends `culprit/`; `harness/` gains only the postmortem-input backfill generator.** New persistence is **additive** and does not touch `signals` or the M3 columns: three resolution fields on `incidents` (`resolved_at` timestamptz, `fixing_sha` varchar(40) nullable, `resolution_source` varchar(16) nullable — `discord|sns_ok|manual|sentry_quiet`) and one new sibling table **`postmortems`** (`incident_id` unique FK, `slug`, `path`, `branch`, `title`, `body` text, `pr_url` nullable, `pr_number` nullable, `state` — `drafted|opened`, `created_at`). Modeling the draft as its own table mirrors `evidence`/`jobs` (`culprit/models.py:116-150`), keeps `incidents` lean, and gives idempotency a home (unique `incident_id` = one PR per outage). One Alembic migration, up/down clean.

2. **Resolution is one deterministic core reached by three converging triggers.** `culprit/resolution.py::resolve_incident(session, incident, *, source, github)` is the single writer of resolution state: set `status="resolved"`, `resolved_at`, `resolution_source`, capture the fixing commit (decision 3), re-render the living brief with `resolved=True` (the M3 brief already supports it, `culprit/brief.py:106`), and return the incident. It is idempotent — resolving an already-resolved incident is a no-op that never re-captures or re-posts. The three triggers are thin adapters over this core:
   - **Operator/eval** — `POST /incidents/{id}/resolve` (a new app route) and `culprit resolve <id>` (CLI). This is the always-available, always-deterministic path the eval drives; it maps to "a maintainer typed resolve."
   - **Auto-detect (infra)** — a CloudWatch alarm delivered to the existing `/ingest/sns` whose `Message.NewStateValue == "OK"` (the `ALARM → OK` transition) resolves the incident that alarm's `ALARM` signal joined. Deterministic and fixture-testable (an OK-state SNS Notification is just another synthesized fixture). Handled inside `culprit/ingest/sns.py`, which already parses the alarm state JSON.
   - **Discord-native** — a signed Discord **interaction** (`/resolve` slash command) to a new `POST /discord/interactions`, Ed25519-verified over the raw body (decision 5). This is the "bot command" from HANDOFF §4.

   **Two triggers are documented as live-only extensions, not built deterministically:** the **reaction** path (✅ on the brief) requires a Discord **Gateway** websocket connection (webhooks/interactions are request/response only) and the **"Sentry issue quiet post-deploy"** heuristic is time-based and awkward to fixture; both are described in `docs/postmortems.md` with the exact wiring, but the deterministic, eval-scored triggers are the interaction + the SNS-OK transition + the operator path. Honest scoping over a fake reaction test.

3. **The fixing commit is captured from the deploy feed — or is honestly absent.** At resolution, `_capture_fix_commit` selects the most recent `deploys` row with `run_started_at > incident.opened_at` (the green deploy that shipped the fix/rollback after the outage began), pinning `fixing_sha` to a real deployed SHA — the deploy-feed-is-truth stance (`HANDOFF §4`; `culprit/pipeline.py:112-134` already resolves windows from the same feed). **When no post-open deploy exists** (an infra fault fixed by restarting Redis / scaling the task — no code shipped), `fixing_sha` stays `NULL` and the postmortem states "**resolved via infrastructure remediation — no fixing commit**." This is the fix-side parallel to culprit abstention: not every resolution has a code fix, and saying so honestly is the design, not a gap.

4. **Postmortem assembly is deterministic; the LLM only phrases prose.** `culprit/postmortem.py::build_postmortem(incident, *, signals, deploys, evidence, jobs, thread=None)` assembles a `PostmortemDraft` (frozen dataclass: `slug`, `path`, `branch`, `title`, `body`, `frontmatter`, `pr_request`) **entirely from persisted rows** — `incidents.diagnosis` (hypotheses + `runbook_id` + impact snapshot, `culprit/diagnosis.py:64-80`), the audit-trail tables (the timeline), `fixing_sha`, and an optional chat thread. The Markdown skeleton is fixed (decision 6); the LLM writes the "Summary" paragraph and may synopsize the "Discussion" — never a fact, a SHA, a number, or a section. `slug`/`path`/`branch` derive deterministically from `opened_at` + a sanitized title (`postmortems/YYYY-MM-DD-<slug>.md`, branch `culprit/postmortem-<incident_id>-<slug>`), so re-drafting is byte-stable and idempotent. The impact line reuses `Impact.render()` verbatim (`culprit/impact.py:47-60`) so methodology stays stated on every number.

5. **The Discord read path is a gated provider that mirrors `LogsProvider`.** `culprit/discord_read.py` defines `ThreadReader` (Protocol: `enabled`, `async read(channel_id, root_message_id) -> list[dict]`) with `DiscordThreadReader` (httpx `GET /channels/{id}/messages`, `Authorization: Bot <token>`, gated on `DISCORD_BOT_TOKEN` — a **read-scoped** bot credential, the read-only stance applied to chat) and `FixtureThreadReader` (reads `fixtures/discord/*.json` — offline eval/demo). Exactly the `culprit/cloudwatch.py:23-46` shape. The thread is the human-narrative half of the postmortem (`HANDOFF §4`); absent token → the "Discussion" section is omitted cleanly (inert-when-keyless), and the doc is still complete without it (the completeness metric does not require it).

6. **The postmortem Markdown is a fixed, commodity skeleton grounded in tCF reality.** Frontmatter (`title`, `date`, `incident_id`, `severity`, `status`, `culprit`, `fixing_commit`) + sections in order: **Summary** (LLM-phrased over facts) · **Impact** (`Impact.render()`, method stated) · **Timeline** (deploy shipped → first signal → incident opened → brief posted → resolved / fix shipped, from the audit trail) · **Root cause — ranked hypotheses** (from `diagnosis.hypotheses`, confidence + evidence ids; or "No code culprit — looks infrastructural") · **Resolution** (fixing commit or infra-remediation; `resolution_source`) · **Suggested runbook** (offer-only, from `diagnosis.runbook_id`) · **Discussion** (thread, gated) · **Similar past incidents** (from the M3 brief's `similar`, optional) · a footer stating **Culprit drafted this; humans review and merge — it never publishes unilaterally.** This matches the incident.io/Rootly/PagerDuty commodity format (HANDOFF §3 "Postmortem drafting: Reliable — commodity GA feature") while carrying Culprit's abstention/offer-only signatures.

7. **The GitHub App is the single new write permission, scoped to branch + file + PR on the fork.** `culprit/github_app.py::GitHubAppWriter` mints an App JWT (RS256 over `{iat, exp, iss: app_id}` — via `pyjwt`, a bounded generic primitive, not a vendor SDK; the M3 "no vendor SDK, httpx for the API" spirit), exchanges it for a short-lived **installation token**, then via httpx REST: get the base branch SHA → `POST /git/refs` (create `culprit/postmortem-…` branch) → `PUT /contents/postmortems/…md` (create the file on that branch) → `POST /pulls` (open the PR into the default branch). **It never calls `PUT /pulls/{n}/merge` — merge is not in the code path at all** (the permanent offer-only stance; the App is not even granted a merge-implying scope beyond `pull_requests:write` needed to open). Gated on `GITHUB_APP_ID` + `GITHUB_APP_PRIVATE_KEY` + `GITHUB_APP_INSTALLATION_ID`; absent → the writer is inert and the pipeline falls back to **dry-run** (decision 8). The read client (`culprit/github_api.py`) is untouched and never handed write scope.

8. **Dry-run is the default; the live PR is one gated test.** `culprit/postmortem.py` always produces the full `PostmortemDraft` (path + branch + title + body + the exact PR request payload) without any network call. `POST /incidents/{id}/resolve` / `culprit postmortem <id>` render + persist the draft to the `postmortems` table in **`drafted`** state by default (`POSTMORTEM_DRY_RUN=true`); only with the App configured **and** dry-run off does `GitHubAppWriter` open the PR and flip the row to `opened` with its `pr_url`. This is what makes the eval scoreable without a live write on every run — the completeness metric reads the rendered `body`, and a single gated live test exercises the real push against a **sandbox branch of the fork** and cleans up (closes the PR + deletes the branch). Idempotency: a `drafted`/`opened` row for the incident short-circuits a second open.

9. **The postmortem-completeness metric is deterministic, LLM-free, and honestly denominated.** `culprit/eval/postmortem_eval.py::evaluate_postmortem_completeness` replays every incident-producing run (the M3 replay, unchanged), lets the pipeline persist `incidents.diagnosis`, resolves the incident with its synthesized **fix-deploy** fixture (decision 11), assembles the dry-run draft, and scores a **structural checklist** over the rendered `body`: has a **Timeline**, a **culprit-or-explicit-abstention** root-cause line, an **impact line with a stated method**, **≥1 ranked hypothesis**, and a **fixing-commit-or-honest-absence**. N = **21** (every incident-producing run; the baseline's non-incident correctly yields no postmortem). Reported per-run and rolled up; run-to-run identical (the assembly is deterministic and the "Summary" LLM sentence is excluded from the check). This is a *new eval section*, not a change to the culprit/abstention numbers.

10. **Anti-leakage is re-asserted at every new boundary.** The postmortem is built from **persisted incident data only** — `incidents.diagnosis`, the audit-trail rows, `fixing_sha`, the thread fixture — **never** from `is_culprit`/`ground_truth`/`culprit_sha`; `culprit/eval/score.py` (and the new completeness scorer's *labels*, of which it has none — it is a structural check) never leak into assembly. The synthesized thread + fix-deploy fixtures carry **no fault identity**: a fix-deploy's `head_sha` is a real fork SHA (the rollback/forward-fix target) with no `is_culprit` marker, and the thread messages are generic on-call chatter that never names the fault or the culprit SHA (the `head_branch: master` / generic-alarm-name precedent, `HANDOFF-M3 §3`). `score.py` remains the only ground-truth reader.

11. **Fault → postmortem-input fixtures reuse the 22-run corpus, deployfeed-grade.** `harness/discordfeed.py` (+ `harness/discord_inputs/` vendored message schema; `culprit-harness backfill-postmortem-inputs`) generates, per incident-producing run: (a) a **fix-deploy** fixture — a `workflow_run` "AWS Deployment" (reusing `harness/deployfeed.py`) whose `head_sha` is the honest fix target (**code faults → the run's `base_sha`**, i.e. a rollback to the pre-fault commit, a real fork SHA; **infra faults → none**, so `fixing_sha` stays NULL) and whose `run_started_at` is after the run's `injected_at`; and (b) a **Discord thread** fixture — the real Discord message-list JSON schema (vendored from Discord's API docs) with generic on-call messages, synthesized ids/timestamps, `"reconstructed": true`. `runrecord.py` gains `fix_deploy:` and `thread:` link fields (the `deploy:`/`sns:` precedent, `harness/runrecord.py:57-65`); `fixtures/discord/PROVENANCE.md` documents every synthesized field + the live-capture path (a real resolved incident's real thread replaces the fixture in place). No new ground truth is introduced — the fix target and thread are shape, not labels.

## Corpus & eval deltas (the N math)

All 22 recorded runs stay in the deterministic culprit/abstention eval **unchanged** (M4 adds no culprit numbers). M4 adds one deterministic section + two gated ones, reusing the same corpus:

| Section | N | What is measured | Gated? |
|---|---|---|---|
| Postmortem completeness (dry-run) | **21** | every incident-producing run's rendered doc has: timeline · culprit-or-abstention · impact-with-method · ≥1 hypothesis · fix-commit-or-honest-absence | No (deterministic, LLM-free) |
| Fixing-commit capture | 21 | code incidents (18) capture a fixing `base_sha`; infra incidents (3) honestly capture *none* | No |
| Resolution auto-detect (SNS `OK`) | 3 | `redis-down`, `db-stopped`, `gunicorn-oom` OK-transition fixtures resolve their open incident | No |
| Live PR open (sandbox) | 1 | one gated push → a real PR to a throwaway fork branch, then cleaned up | Yes (GitHub App) |
| Postmortem narrative fidelity | 21 | LLM "Summary" adds no new SHA/number/section absent from the deterministic body | Yes (Anthropic) |

Baseline (`benign-deploy-baseline-w4`) produces no incident → **no postmortem** (correct silence; the completeness N stays 21, not 22).

Fixing-commit / thread fixture mapping (drives `discordfeed.py`; reuses the M3 fault→run map):

| Run class | Fix-deploy fixture (`fixing_sha`) | Resolution trigger in eval |
|---|---|---|
| 10 Sentry-visible code faults (`template-noreversematch`, `search-fielderror-500`, `landing-import-crash`, `vote-duplicate-integrityerror`, `bad-migration-drop-semester-season`; w1/w4) | rollback to `base_sha` | operator `resolve` (deterministic) |
| 8 SNS-silent code faults (`n-plus-one`, `cartesian-join`, `search-silent-zero-results`, `bad-migration-drop-trigram`; w1/w4) | rollback to `base_sha` | operator `resolve` |
| 3 infra faults (`redis-down`, `db-stopped`, `gunicorn-oom`) | **none** — `fixing_sha = NULL` | SNS `ALARM → OK` auto-detect |
| 1 baseline (`benign-deploy-baseline-w4`) | n/a (no incident) | — |

## Files to Change

| File | Action | Why |
|---|---|---|
| `pyproject.toml` | UPDATE | Add `pyjwt>=2.8,<3` (GitHub App JWT). Discord interactions/read + Ed25519 use existing `cryptography` + `httpx` — no new SDK dep (M3 Voyage precedent) |
| `culprit/config.py` | UPDATE | `github_app_id`, `github_app_private_key`/`_path`, `github_app_installation_id`, `postmortems_repo` (default = fork), `postmortems_dir` (default `postmortems`), `postmortem_dry_run` (default `true`), `discord_bot_token`, `discord_public_key` (interactions verify), `discord_incident_channel_id` — all optional/inert (decision 5,7) |
| `culprit/models.py` + `migrations/versions/<new>` | UPDATE/CREATE | `incidents.resolved_at/fixing_sha/resolution_source`; new `postmortems` table; **no `signals` change** (decision 1) |
| `culprit/resolution.py` | CREATE | `resolve_incident` core + `_capture_fix_commit` (decisions 2,3) |
| `culprit/ingest/sns.py` | UPDATE | Detect `NewStateValue == "OK"` → `resolve_incident(source="sns_ok")` (decision 2) |
| `culprit/discord_verify.py` | CREATE | Ed25519 verify of a Discord interaction over the raw body (decision 5) |
| `culprit/ingest/discord.py` | CREATE | Parse a `/resolve` interaction → incident id; PING→PONG handshake (decision 2) |
| `culprit/discord_read.py` | CREATE | `ThreadReader` Protocol + `DiscordThreadReader` (gated) + `FixtureThreadReader` (decision 5) |
| `culprit/postmortem.py` | CREATE | Deterministic `build_postmortem` → `PostmortemDraft`; slug/path/branch derivation; dry-run render (decisions 4,6,8) |
| `culprit/github_app.py` | CREATE | `GitHubAppWriter`: JWT→install token→branch+file+PR; never merges; gated/inert (decision 7) |
| `culprit/llm.py` | UPDATE | `phrase_postmortem(sections)` — prose only, never a fact (decision 4) |
| `culprit/app.py` | UPDATE | `POST /incidents/{id}/resolve`, `POST /discord/interactions`; background draft (dry-run default) |
| `culprit/cli.py` | UPDATE | `culprit resolve <id>`, `culprit postmortem <id> [--open]` (decision 8; mirrors `culprit/cli.py:102-127`) |
| `harness/discordfeed.py` + `harness/discord_inputs/` | CREATE | Synthesized thread + fix-deploy fixtures + vendored schema; `backfill-postmortem-inputs` (decision 11) |
| `harness/cli.py`, `harness/runrecord.py` | UPDATE | `backfill-postmortem-inputs` command; run records link `fix_deploy:`/`thread:` (decision 11) |
| `fixtures/discord/` + `PROVENANCE.md`, `fixtures/github/workflow_run/` (fix deploys) | CREATE | 21 thread fixtures + 18 fix-deploy fixtures + full provenance/live-capture path (decision 11) |
| `culprit/eval/postmortem_eval.py` | CREATE | Dry-run completeness metric (N=21) + resolution auto-detect check + gated live-PR + narrative-fidelity (decision 9) |
| `culprit/eval/driver.py`, `score.py`, `cli.py` | UPDATE | Wire the postmortem sections into the report; own N each; `--no-gated` still deterministic-only |
| `docs/postmortems.md` | CREATE | The M4 runbook: triggers, assembly, dry-run vs live, the reaction/Sentry-quiet live extensions |
| `docs/github-app.md` | CREATE | The single write-permission ask (contents + pull-requests on the fork) + install steps — hand-to-maintainer artifact (mirrors `docs/aws/aws-access.md`) |
| `docs/pipeline.md` | UPDATE | M4 section: resolution, postmortem, GitHub App, new eval denominators |
| `tests/test_resolution.py`, `test_postmortem.py`, `test_github_app.py`, `test_discord_verify.py`, `test_discord_read.py`, `test_ingest_discord.py`, `test_discordfeed.py`, `test_postmortem_eval.py` | CREATE | TDD per task (network/secret-gated where credential-dependent) |
| `tests/test_corpus.py`, `test_app.py`, `test_cli.py`, `test_eval.py`, `test_ingest_sns.py` | UPDATE | Thread/fix-deploy fixture invariants; new routes; new CLI verbs; SNS-OK resolution; eval sections |

## Tasks

### Task 1: Resolution model + core resolver (zero external)
- **Action**: Alembic migration adds `incidents.resolved_at` (timestamptz), `incidents.fixing_sha` (varchar40, null), `incidents.resolution_source` (varchar16, null) and the `postmortems` table (decision 1). `culprit/resolution.py::resolve_incident(session, incident, *, source, github=None)` sets `status="resolved"`, `resolved_at`, `resolution_source`; `_capture_fix_commit` picks the most recent deploy with `run_started_at > incident.opened_at` → `fixing_sha` (or `None` for infra, decision 3); idempotent (already-resolved → no-op). Wire `POST /incidents/{id}/resolve` (`culprit/app.py`) + `culprit resolve <id>` (`culprit/cli.py`); both re-render the brief with `resolved=True`.
- **Mirror**: additive columns + sibling table (`culprit/models.py:70-77,116-150`); idempotency on state (`culprit/correlation.py:82-83`); deploy-feed-is-truth (`culprit/pipeline.py:112-134`).
- **Validate**: `uv run pytest tests/test_resolution.py` — resolving flips `status`/`resolved_at`/`resolution_source`; a post-open deploy is captured as `fixing_sha`; an infra incident with no post-open deploy → `fixing_sha is None`; double-resolve is a no-op; migration up/down clean.

### Task 2: Auto-detect + Discord-native triggers
- **Action**: In `culprit/ingest/sns.py`, an alarm Notification with `Message.NewStateValue == "OK"` resolves the incident its `ALARM` signal joined via `resolve_incident(source="sns_ok")` (decision 2) — still idempotent, still verify-then-parse (`culprit/app.py:150-153`). `culprit/discord_verify.py` verifies a Discord interaction's Ed25519 signature over `timestamp + raw_body` (via `cryptography`); `culprit/ingest/discord.py` handles the `PING→PONG` handshake and parses a `/resolve` command's incident id; `POST /discord/interactions` wires it (401 on bad signature; 400 on missing headers). Document the reaction-gateway + Sentry-quiet paths as live-only extensions in `docs/postmortems.md`.
- **Mirror**: verify-then-parse + header-dispatch + SSRF/allowlist stance (`culprit/app.py:126-170`, `culprit/sns_verify.py:43-120`); 401-on-bad-signature.
- **Validate**: `uv run pytest tests/test_ingest_sns.py tests/test_discord_verify.py tests/test_ingest_discord.py` — an `OK`-state SNS fixture resolves the open incident (and an `ALARM` one still opens/joins); a validly-signed `/resolve` interaction resolves; a tampered interaction body → 401; a `PING` → `PONG`.

### Task 3: Deterministic postmortem assembly (dry-run)
- **Action**: `culprit/postmortem.py::build_postmortem(...)` assembles a frozen `PostmortemDraft` (slug/path/branch/title/frontmatter/body/pr_request) from `incidents.diagnosis`, the audit-trail rows, and `fixing_sha` — the fixed skeleton of decision 6, reusing `Impact.render()` (`culprit/impact.py:47-60`) and the `diagnosis.hypotheses` shape (`culprit/diagnosis.py:64-80`). Deterministic slug/branch from `opened_at` + sanitized title; no network. Persist to the `postmortems` table in `drafted` state.
- **Mirror**: deterministic-decides (`culprit/diagnosis.py:1-9`); persisted-diagnosis-is-the-input (`culprit/pipeline.py:305-315`); byte-stable synthesis (`harness/deployfeed.py:1-47`).
- **Validate**: `uv run pytest tests/test_postmortem.py` — a code-culprit incident's body contains all five required sections + the culprit SHA + a fixing-commit line; an abstention incident renders "No code culprit — looks infrastructural" and (if infra) "no fixing commit — infra remediation"; the render is byte-identical across two calls; assembly reads **no** ground-truth field.

### Task 4: Discord thread read (the human-narrative half)
- **Action**: `culprit/discord_read.py` — `ThreadReader` Protocol + `DiscordThreadReader` (httpx `GET /channels/{id}/messages`, `Authorization: Bot`, gated on `DISCORD_BOT_TOKEN`) + `FixtureThreadReader` (reads `fixtures/discord/*.json`), exactly the `culprit/cloudwatch.py:23-46` shape. `build_postmortem` accepts an optional `thread` and renders a "Discussion" section; absent/empty → the section is omitted and the doc is still complete.
- **Mirror**: provider-behind-interface + `enabled`-gating + `Fixture*` twin (`culprit/cloudwatch.py:23-46`); httpx client style (`culprit/github_api.py`); inert-when-keyless (`culprit/config.py:26-71`).
- **Validate**: `uv run pytest tests/test_discord_read.py tests/test_postmortem.py` — the fixture reader yields the thread messages; the postmortem includes "Discussion" with the thread when present and omits it cleanly when the reader is inert; gated live test (ask the user for the bot token before skipping) reads a real channel.

### Task 5: LLM narrative phrasing (deterministic decides, LLM phrases)
- **Action**: `culprit/llm.py::phrase_postmortem(sections)` writes the "Summary" paragraph (and optionally a Discussion synopsis) over the already-decided facts — never a new SHA, number, or section (the `phrase_diagnosis` contract, `culprit/llm.py:123-152`). Gated on `ANTHROPIC_API_KEY`; absent → the deterministic fallback Summary (a templated sentence over the same facts) is used, so the doc is always complete.
- **Mirror**: LLM-phrases-only + never-scored (`culprit/llm.py:1-9,123-152`).
- **Validate**: `uv run pytest tests/test_postmortem.py::test_narrative` — keyless render uses the deterministic Summary and still passes the completeness checklist; gated live test: with a key, the Summary is prose but the body's SHAs/numbers/sections are unchanged (structural assertions still hold) — ask the user for the key before skipping.

### Task 6: GitHub App write path (the ONE write permission)
- **Action**: `culprit/github_app.py::GitHubAppWriter` — mint the App JWT (`pyjwt`, RS256), exchange for an installation token, then create the branch → write `postmortems/…md` → open the PR into the default branch, all via httpx REST (decision 7). **No merge call exists in the module.** Gated on the three App settings; absent → inert (pipeline stays dry-run). `pyproject.toml` gains `pyjwt`. Idempotent against the `postmortems` table (a `drafted`/`opened` row short-circuits).
- **Mirror**: httpx client + separate-from-read-client (`culprit/github_api.py:1-11,52-108`); env-gated inert (`culprit/config.py:26-71`); one-per-outage idempotency (`culprit/models.py:97`).
- **Validate**: `uv run pytest tests/test_github_app.py` — with a mocked httpx transport, the writer issues exactly ref-create + contents-put + pulls-create and **never** a merge call; dry-run/inert returns the PR request without any network call; a second draft for the same incident opens no second PR. (The real push is Task 8's gated live test.)

### Task 7: Synthesized postmortem-input fixtures (thread + fix-deploy), deployfeed-grade
- **Action**: `harness/discordfeed.py` + `harness/discord_inputs/` (vendored Discord message-list schema) + `culprit-harness backfill-postmortem-inputs` (decision 11): per incident-producing run, emit a **fix-deploy** `workflow_run` fixture (reusing `harness/deployfeed.py`; code faults → `base_sha` rollback with a post-injection `run_started_at`; infra → none) and a **Discord thread** fixture (generic on-call chatter, synthesized ids/timestamps, `reconstructed: true`). `runrecord.py` gains `fix_deploy:`/`thread:` links; `fixtures/discord/PROVENANCE.md` documents every synthesized field + the live-capture path. Extend `tests/test_corpus.py` (no orphans; thread/fix fixtures never name the fault or the culprit SHA).
- **Mirror**: `harness/deployfeed.py:1-47` + `harness/snsfeed.py` end-to-end (schema template, deterministic synthesis, provenance, key-parity test); backfill-CLI (`harness/cli.py:120-190`).
- **Validate**: `uv run pytest tests/test_discordfeed.py tests/test_corpus.py` — key-parity vs the vendored Discord schema; regeneration byte-stable; every fix-deploy `head_sha` resolves on the fork and equals `base_sha` for code faults; anti-leakage invariants green (no fault identity in any fixture).

### Task 8: Eval — dry-run completeness + resolution auto-detect + gated live PR
- **Action**: `culprit/eval/postmortem_eval.py`: replay each incident-producing run (M3 replay unchanged), persist `diagnosis`, resolve (operator for code, SNS-`OK` for infra), assemble the dry-run draft, and score the **completeness checklist** (decision 9) — N=21, plus the fixing-commit-capture and SNS-`OK`-resolution checks. Add a **gated live-PR** test that opens a real PR to a sandbox branch of the fork with the App and then cleans up (close PR + delete branch), and the gated **narrative-fidelity** check. Wire all into `culprit/eval/driver.py`/`score.py`/`cli.py` with own N each; `--no-gated` stays deterministic-only.
- **Mirror**: replay anti-leakage (`culprit/eval/replay.py:1-11`); honest per-class N + gated sections (`culprit/eval/score.py:143-202`, `driver.py:36-62`).
- **Validate**: `uv run culprit eval` — the completeness section shows 21/21 (or the honest number) with every subsection denominated; deterministic sections identical run-to-run; anti-leakage assertions extended (assembly reads no labels; fixtures carry no fault identity); the M3 culprit/abstention numbers are unchanged. Ask the user for the App/Anthropic creds before skipping the gated live-PR/narrative tests.

### Task 9: Docs + access pack + CI
- **Action**: `docs/github-app.md` — the exact single write-permission ask (a GitHub App with `contents: write` + `pull_requests: write` on `IshanA2007/theCourseForum2` only, no merge, no other scope) + install/setup steps, the hand-to-maintainer artifact (mirrors `docs/aws/aws-access.md`). `docs/postmortems.md` — triggers (operator/SNS-OK/interaction) + the reaction-gateway & Sentry-quiet live extensions + dry-run vs live + a sample rendered postmortem. Update `docs/pipeline.md` (M4 env vars, resolution, postmortem, new denominators). `culprit postmortem <id> [--open]` CLI. CI stays secretless-green.
- **Mirror**: `docs/aws/aws-access.md` access-pack style; `docs/pipeline.md` runbook style; `culprit/cli.py:102-127` subparser style.
- **Validate**: `python -m json.tool` on any JSON artifact; `uv run ruff check . && uv run ruff format --check . && uv run pytest` green locally + secretless CI; `uv run culprit eval` green; `culprit postmortem <id>` renders a sample dry-run doc.

## Validation
```bash
export PATH="$HOME/.local/bin:$PATH"
set -a; source .env; set +a
export GITHUB_TOKEN=$(gh auth token)                       # read path (unchanged)
uv sync && uv run ruff check . && uv run ruff format --check .
docker compose up -d db && uv run culprit migrate          # + the M4 resolution/postmortems migration
uv run pytest                                              # offline+DB suite; gated tests need App/bot/LLM creds
uv run culprit eval                                        # 22-run culprit numbers UNCHANGED + postmortem completeness N=21
uv run culprit eval --no-gated                            # deterministic headline + completeness (no LLM/App)
# resolve → dry-run draft:
uv run culprit resolve 1 && uv run culprit postmortem 1   # renders postmortems/YYYY-MM-DD-slug.md to stdout (no push)
# gated live PR (ask the user first; needs the GitHub App):
POSTMORTEM_DRY_RUN=false uv run culprit postmortem 1 --open   # opens ONE PR to a sandbox fork branch; humans merge
```

## Risks
| Risk | Likelihood | Mitigation |
|---|---|---|
| A live write (PR) misfires — wrong repo/branch, or an accidental merge | Low, High impact | Merge is **not in the code path** (decision 7); dry-run is default (decision 8); the write client is separate from the read client and scoped to one fork; the live path is one gated test against a sandbox branch that cleans up; idempotency (unique `incident_id`) prevents duplicate PRs |
| Resolution mis-fires — an alarm flap (`OK`) closes a still-broken incident | Medium | `sns_ok` resolves only the incident the matching `ALARM` joined and is idempotent; a later `ALARM` can re-open per correlation; the operator `resolve` is always available to correct it; documented tradeoff |
| Fixing-commit capture is wrong (picks an unrelated later deploy) | Medium | Honest by construction: `fixing_sha` is the most recent post-open deploy or **none**; the postmortem states the method and never asserts causation for the fix; infra faults correctly capture *no* fix (decision 3) |
| Discord chat thread unavailable (no bot token) or empty | Medium | The thread is the *optional* human-narrative half; absent → "Discussion" omitted and the doc is still complete (completeness N does not require it); gated + inert-when-keyless (decision 5) — ask the user for the bot token before skipping the live read |
| Synthesized thread/fix-deploy shape drifts from real deliveries | Medium | Vendored real schemas + key-parity tests (deployfeed/snsfeed precedent); `PROVENANCE.md` documents deltas + the live-capture path — a real resolved incident upgrades the fixtures in place without touching the service |
| GitHub App JWT/token handling is a new credential surface | Medium | `pyjwt` (audited primitive) not hand-rolled crypto; short-lived installation token minted per write; private key gitignored + env-gated + inert by default; the App's granted scope is the *entire* mitigation — it literally cannot merge or touch other repos |
| Discord interactions endpoint becomes an abuse surface | Medium | Mandatory Ed25519 signature verification over the raw body (401 on failure); `PING` handshake only; no outbound fetch from the interaction path (no SSRF surface) |
| LLM "Summary" invents a fact or a different culprit | Medium | Phrasing-only contract + deterministic fallback; the completeness metric is computed over the deterministic body, not the Summary; narrative-fidelity is its own gated check asserting no new SHA/number/section |
| Postmortem eval needs a live PR on every run | Certain (by design, avoided) | Dry-run renders the Markdown + the PR request without pushing; completeness is scored on the rendered body; exactly one gated live PR proves the real path |
| Scope creep into auto-merge / unilateral publish | Low | Restated as a non-negotiable: Markdown PR only, humans merge; no merge code exists; footer states it in the doc itself |

## Forward visibility → Milestone 5
- The **postmortem-completeness N=21** joins the M3 culprit/abstention/runbook/similar numbers as a Phase-5 headline: "every resolved incident produces a complete, human-reviewable postmortem draft."
- `docs/github-app.md` is the **third instrumentation artifact** for the pitch (after the `sentry-sdk` PR and `alarms-proposal.tf`): the exact, minimal write-permission ask a maintainer approves in one click — the offer-only stance made concrete one last time.
- The dry-run → live-PR seam is the Mode-B → Mode-A analog for writes: everything proven on fixtures, one credential flips it live; a real resolved incident's real thread + real fix deploy upgrade `fixtures/discord/` in place (the documented deployfeed-style path).
- Nothing in M4 requires live AWS; M5's remaining ask stays the read-only IAM grant (M3's `docs/aws/`), now joined by the one GitHub-App write grant.

## Acceptance
- [ ] Resolving an incident (operator `resolve`, SNS `ALARM → OK`, or a signed Discord `/resolve` interaction) flips `status`, stamps `resolved_at`/`resolution_source`, captures the fixing commit from the deploy feed (or honestly `none` for infra), and is idempotent — with **no change to the `signals` schema**
- [ ] The postmortem is assembled **deterministically** from `incidents.diagnosis` + the `deploys`/`signals`/`evidence`/`jobs` audit trail + measured impact + the fixing commit + the (optional, gated) Discord thread; the LLM phrases the Summary prose only and computes no fact
- [ ] Every incident-producing run's dry-run postmortem passes the completeness checklist — timeline · culprit-or-abstention · impact-with-method · ≥1 ranked hypothesis · fix-commit-or-honest-absence — at N=21, reported with its denominator; deterministic run-to-run
- [ ] The GitHub App opens a branch + `postmortems/YYYY-MM-DD-slug.md` + a PR on the fork as the **single new write permission** (contents + pull-requests, that repo only); **no merge call exists in the code path**; one PR per incident (idempotent); dry-run is the default and the live push is one gated, cleaned-up test
- [ ] **Offer-only / humans-merge / never publish unilaterally** is preserved end-to-end — Markdown PR only, no auto-merge, stated in the doc footer and enforced by the absent merge path
- [ ] **Anti-leakage**: assembly reads only persisted incident data (never `is_culprit`/`ground_truth`/`culprit_sha`); the synthesized thread/fix-deploy fixtures name no fault or culprit SHA; `culprit/eval/score.py` remains the only ground-truth reader; the M3 culprit/abstention numbers are unchanged
- [ ] **Deterministic decides, LLM phrases**: the completeness eval is LLM-free and reproducible; the narrative-fidelity check is a separate gated section with its own N
- [ ] **Zero live AWS** anywhere in M4; the read path stays read-only; `docs/github-app.md` + `docs/postmortems.md` exist as hand-to-maintainer artifacts
- [ ] Patterns mirrored (deployfeed/snsfeed provenance, provider-behind-interface, env-gated inert integrations, verify-then-parse, TDD); ruff + pytest green locally and in secretless CI
