# Culprit — Postmortem Generator (Milestone 4)

When an incident **resolves**, Culprit turns its persisted diagnosis into a
**postmortem Markdown PR** on the fork. Culprit drafts; a human merges. This is the
M4 layer on top of the M3 diagnosis (`incidents.diagnosis`) and the
deploy/signal/evidence audit trail.

## The trigger — three converging resolution paths

All three flow through one deterministic core, `culprit/resolution.py::resolve_incident`
(sets `status=resolved`, `resolved_at`, `resolution_source`, and captures the
**fixing commit** from the deploy feed — the most recent deploy after the incident
opened, or honestly *none* for an infra remediation):

1. **Operator** — `POST /incidents/{id}/resolve` or `culprit resolve <id>`. The
   always-available, deterministic path (also what the eval drives).
2. **Auto-detect (infra)** — a CloudWatch alarm's `ALARM → OK` transition on the
   existing `POST /ingest/sns` resolves the incident that alarm opened/joined.
3. **Discord-native** — a signed `/resolve` slash-command interaction to
   `POST /discord/interactions` (Ed25519-verified over the raw body, like the SNS
   X.509 boundary; a `PING` gets a `PONG`).

**Live-only extensions (documented, not in the deterministic eval):** the ✅
**reaction** path needs a Discord **Gateway** websocket (webhooks/interactions are
request/response only); the **"Sentry issue quiet post-deploy"** heuristic is
time-based and awkward to fixture. Both wire onto the same `resolve_incident` core
when a live deployment wants them.

## Assembly — deterministic decides, the LLM only phrases

`culprit/postmortem.py::build_postmortem` renders a fixed skeleton from **persisted
incident data only** (never a ground-truth label): the diagnosis hypotheses +
offered runbook + impact snapshot, the deploy/signal timeline, the fixing commit,
and the (optional, gated) Discord chat thread. The LLM writes the **Summary**
paragraph only — never a SHA, a number, or a section. slug/path/branch are
deterministic, so a re-draft is byte-stable and the write is idempotent.

Sections: **Summary** · **Impact** (method stated on every number) · **Timeline** ·
**Root cause — ranked hypotheses** (or "No code culprit — looks infrastructural") ·
**Resolution** (fixing commit, or honest infra remediation) · **Suggested runbook**
(offer-only) · **Discussion** (the chat thread, when available) · a footer stating
Culprit drafted it and a human merges.

## Dry-run vs. live

- **Dry-run (default, `POSTMORTEM_DRY_RUN=true`)** — render the Markdown + the PR
  request, push nothing. `culprit postmortem <id>` prints it; the eval scores it.
- **Live** — with the GitHub App configured and dry-run off, `culprit postmortem
  <id> --open` opens **one** PR (branch → file → PR; **never a merge**). See
  [`docs/github-app.md`](github-app.md). Idempotent: one PR per incident.

```bash
uv run culprit resolve 1                 # resolve + capture the fixing commit
uv run culprit postmortem 1              # dry-run: print the rendered Markdown
POSTMORTEM_DRY_RUN=false \
  uv run culprit postmortem 1 --open     # open the PR (needs the GitHub App)
```

## Eval — completeness without a live write on every run

`culprit eval` adds a deterministic **postmortem-completeness** section (LLM-free,
reproducible): every incident-producing run is replayed → resolved → drafted in
dry-run, and the rendered body is scored for **timeline · culprit-or-abstention ·
impact-with-method · ≥1 hypothesis · fix-commit-or-honest-absence**.

| Section | N | Result |
|---|---|---|
| Postmortem completeness (dry-run) | 21 | 21/21 complete |
| Fixing-commit captured (code faults) | 18 | 18/18 (rollback to `base_sha`) |
| Resolved via SNS `ALARM→OK` (infra) | 3 | 3/3 |
| Live PR open (sandbox) | 1 | gated on the GitHub App |
| Narrative fidelity (LLM Summary adds no fact) | — | gated on Anthropic |

The 22nd run is the benign baseline: it produces no incident, so it correctly
yields **no** postmortem (completeness N stays 21). Inputs reuse the 22-run corpus:
a synthesized rollback fix-deploy per code fault + a generic Discord thread per
incident (see [`fixtures/discord/PROVENANCE.md`](../fixtures/discord/PROVENANCE.md)).

## Sample (dry-run render)

```markdown
---
title: FieldError: cannot resolve keyword 'x'
date: 2026-07-04
incident_id: 7
severity: 2
status: resolved
culprit: abcd1234
fixing_commit: ffffffff
---

# Postmortem: FieldError: cannot resolve keyword 'x'

## Summary

Incident **FieldError: cannot resolve keyword 'x'** (severity 2) was diagnosed with
a likely code culprit (`abcd1234`) and resolved via fixing commit `ffffffff`.

## Impact

**Impact:** ~128 failed request(s) over 12 min (method: Sentry issue.count …) ·
est. ≈37 unique user(s) affected (method: Sentry userCount …)

## Timeline

- 2026-07-04 03:45 UTC — Deploy `rrrrrrrr` shipped (the window head).
- 2026-07-04 03:45 UTC — First signal (sentry/event_alert) fired.
- 2026-07-04 03:45 UTC — Incident opened; Culprit posted the brief.
- 2026-07-04 04:10 UTC — Incident resolved (via manual).
- 2026-07-04 04:10 UTC — Fix `ffffffff` shipped.

## Root cause — ranked hypotheses

1. _[high confidence]_ Likely code culprit: commit abcd1234 — … (evidence #1, #2)

## Resolution

Resolved (detected via **manual**). Fixing commit `ffffffff` shipped after the
incident opened.

## Suggested runbook (offered, not executed)

**Search returns zero / near-zero results** — … _Culprit offers this runbook; it
never executes remediation._

---

*Drafted by Culprit … Culprit never publishes unilaterally and never auto-merges.*
```
