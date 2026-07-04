# `fixtures/discord/` — provenance (M4 postmortem inputs)

Synthesized, deployfeed-grade fixtures that feed the **postmortem generator**
(Milestone 4). Generated deterministically by
[`harness/discordfeed.py`](../../harness/discordfeed.py) via
`culprit-harness backfill-postmortem-inputs`. Two artifacts per resolved incident,
reusing the existing 22-run corpus:

## 1. Discord chat thread — `fixtures/discord/<injected_at>-<8hex>.json`

The incident channel's on-call chatter — the **human-narrative half** the
postmortem joins to the machine timeline (read live by `DiscordThreadReader`; read
offline by `FixtureThreadReader`). One per **incident-producing** run (18 code + 3
infra = 21); the benign baseline produces no incident and gets none.

| Field | Provenance |
|---|---|
| message **schema** | Real Discord `GET /channels/{id}/messages` message shape, vendored at [`harness/discord_inputs/template_message.json`](../../harness/discord_inputs/template_message.json) (`id`, `channel_id`, `author{id,username,global_name}`, `content`, `timestamp`). |
| `content` | **Generic on-call script** (`THREAD_SCRIPT`) — "site is throwing errors", "rolling back per the runbook", "green now, resolving". **Never names the fault, the module, or the culprit sha** (anti-leakage — the SNS/`head_branch:master` precedent). |
| `id` / `channel_id` / `author.id` | Synthesized deterministically from the run id (`_seed_int`) — byte-stable across regeneration. |
| `timestamp` | Synthesized relative to the run's `injected_at` (2-minute cadence). |
| `reconstructed` | `true` — assembled from a schema template, not a real Discord capture. |

## 2. Rollback fix-deploy — `fixtures/github/workflow_run/<injected_at>-fix-<8hex>.json`

A genuine `workflow_run` ("AWS Deployment") that **ships `base_sha`** — the
last-known-good commit — as the rollback that resolved the incident. This is the
**fixing commit** the postmortem cites in its timeline and frontmatter. One per
**code** fault (18); infra faults are fixed by remediation (no code shipped), so
they carry **no** fix-deploy and the postmortem honestly states
`fixing_commit: none — infra remediation`.

| Field | Provenance |
|---|---|
| envelope | The recorder envelope (`received_at`, `source: github`, `resource: workflow_run`, `headers`, `raw_body` latin-1) — the same ingest contract as `harness/deployfeed.py`. |
| `workflow_run.head_sha` | The run's **`base_sha`** — a real fork commit (the pre-fault, last-known-good state a rollback returns to). |
| `workflow_run.run_started_at` | `injected_at + 900s` (a documented detect→decide→rollback delay) — after the fault shipped, so resolution captures it as the fixing commit. |
| `head_commit.message` | Generic `"Roll back to last-known-good <base8>"` — no fault identity. |
| `x-hub-signature-256` | HMAC over the body with the fork's real `CULPRIT_GH_WEBHOOK_SECRET` when that secret is in the environment (parity with `deployfeed`); unsigned otherwise. |
| `reconstructed` | `true`. |

## Path to a real capture

When Culprit runs live for a real resolved incident, the real Discord thread
(read via the bot token) and the real rollback deploy (from the deploy feed)
replace these fixtures **in place** — the documented deployfeed-style upgrade path.
Nothing in the service changes; only the fixture bytes do.

## Anti-leakage invariants (enforced in `tests/test_corpus.py`)

- Every thread / fix-deploy is referenced by exactly one run (no orphans).
- No fixture contains its run's `fault_id` or `culprit_sha`.
- Fix-deploys exist for **exactly** the 18 code faults; threads for **exactly**
  the 21 incident-producing runs.
- All fixtures stamp `reconstructed: true`; regeneration is byte-stable.
