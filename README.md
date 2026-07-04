# Culprit

Culprit is an AI incident-response layer for [theCourseForum](https://github.com/thecourseforum/theCourseForum2), a student-run course-review platform at UVA with 10k+ users a semester and no existing monitoring.

When production breaks, Culprit posts a brief to the team's Discord with the likely culprit commit, a diagnosis backed by cited evidence, an estimated user impact, and the right runbook to fix it. After the incident is resolved, it drafts the postmortem as a pull request. It offers the runbook but never runs it, and it drafts the postmortem PR but never merges it. A human is always on the button.

## What it does

```
  Sentry alert ┐
  CloudWatch   ├─► ingest ─► correlate ─► INCIDENT ─► gather evidence ─► rank culprit
  GitHub deploy┘   (webhooks)  (dedup)               (pinned to the      (or abstain)
                                                      deployed SHA)            │
                                                                               ▼
                          Discord brief  ◄──  diagnosis + impact + runbook + similar
                          (living message)     past incidents (deterministic; LLM phrases)
                               │
                               ▼
                          resolve  ─►  postmortem pull request (timeline + culprit +
                          (bot / auto / manual)   impact + chat thread), humans merge
```

One containerized service plus one Postgres. Everything else is traffic in and out. The pipeline is deterministic for anything countable (impact math, commit windows, verdicts); the language model only phrases and reasons, so the eval stays reproducible.

## Status

Built and verified against a fork of theCourseForum2 plus live GitHub, Anthropic, Voyage, and Discord. It runs end-to-end locally on a recorded and synthesized incident corpus. It is not yet deployed to theCourseForum production, which needs their sign-off on instrumentation and read-only access (Milestone 5).

| # | Milestone | What it delivers | Status |
|---|-----------|------------------|--------|
| 1 | Fault-injection harness | A fork of theCourseForum2 run locally, ~12 labeled faults, real webhook payloads captured as a pytest corpus | Done |
| 2 | Core pipeline | An injected fault produces a deduped Discord brief with a ranked culprit commit (or an abstention) | Done |
| 3 | Diagnosis layer | 12 runbooks, deterministic impact numbers, SNS/CloudWatch ingest for the failures Sentry cannot see | Done |
| 4 | Postmortem generator | Resolving an incident drafts a Markdown postmortem PR to the repo | Done |
| 5 | Eval, harden, pitch | Deploy for theCourseForum with their sign-off | Next |

## The numbers

Computed by `culprit eval` over the full 22-run corpus. These are the honest, interview-defensible numbers; nothing is averaged to look better than it is.

| Metric | Result |
|--------|--------|
| Culprit commit, Sentry-visible code faults (top-1 / top-3) | 10/10 / 10/10 |
| Culprit commit, silent code faults caught only by an alarm (top-1 / top-3) | 4/8 / 5/8 |
| Culprit commit, combined (top-1 / top-3) | 14/18 / 15/18 |
| Correct abstention on infrastructure faults ("no code culprit") | 3/3 |
| False positives on a benign deploy | 0/1 |
| Postmortem completeness (timeline, culprit or abstention, impact with method, hypotheses, fix commit) | 21/21 |

Industry context: state-of-the-art agents fully resolve only 13.8 percent of SRE scenarios (ITBench, ICML 2025), and roughly 27 percent of severe incidents are code bugs (Microsoft, SoCC 2022). Knowing when to say "no code culprit, looks infrastructural" is what separates this from a chatbot wrapper.

## Design principles

1. **Deterministic decides, the LLM phrases.** Every score, count, window, and verdict is computed in code. The language model writes the human-facing narrative and picks a runbook from a fixed list, and its output is never scored by the eval.
2. **Offer-only, humans merge.** Culprit suggests a runbook but never executes remediation, and it drafts a postmortem PR but never merges or publishes it. This matches the whole industry's human-in-the-loop stance.
3. **Rank or abstain, never assert.** Diagnosis is always ranked hypotheses with cited evidence and a confidence band, or an explicit abstention. There is no single asserted root cause.
4. **Honest denominators.** Every eval number ships with its N, per class. Sentry-visible and silent faults are reported separately and combined, never blended to inflate a headline.
5. **Anti-leakage is sacred.** The pipeline sees only what a real webhook would carry. The scorer is the one and only reader of ground truth.
6. **One write permission.** Reads use a public, read-only path. The single write capability is a tightly scoped GitHub App that can open a branch and a PR on the repo, and nothing else.

## Quickstart

```bash
uv sync                                   # install
docker compose up -d db                   # Postgres 17 with pgvector on :5432
uv run culprit migrate                    # apply migrations
uv run pytest                             # run the suite

# the resume numbers (needs a GitHub token for fork reads; gated sections need keys)
export GITHUB_TOKEN=$(gh auth token)
uv run culprit eval

# run the service and see the full loop
uv run culprit serve --port 8010
# POST a recorded webhook, watch a brief post, then:
uv run culprit resolve <incident_id>      # resolve and capture the fixing commit
uv run culprit postmortem <incident_id>   # render the postmortem (dry run)
```

Secrets live in a gitignored `.env` and are all optional: an absent secret makes that integration inert and its tests skip, so the deterministic pipeline runs with none. See [`docs/pipeline.md`](docs/pipeline.md) for every variable.

## Layout

| Path | What |
|------|------|
| `culprit/` | The service: ingest, correlation, evidence, ranking, diagnosis, impact, runbooks, resolution, postmortem, GitHub App writer, CLI |
| `culprit/eval/` | Replay-based scoring over the corpus (the only reader of ground truth) |
| `harness/` | The fault-injection engine and the fixture generators (deploy feed, SNS feed, postmortem inputs) |
| `runbooks/` | 12 offer-only runbooks for theCourseForum's real failure modes |
| `fixtures/` | The recorded and synthesized corpus (Sentry, GitHub, SNS, Discord) |
| `runs/` | Per-scenario run records (base SHA, window commits, ground truth) |
| `migrations/` | Alembic migrations |
| `docs/` | Runbooks and access asks (pipeline, postmortems, github-app, aws) |

## Stack

Python 3.12, FastAPI, Postgres with pgvector, and uv / ruff / pytest / GitHub Actions, chosen to mirror theCourseForum2's toolchain so their team can co-maintain it. Claude (Sonnet for reasoning, Haiku for cheap summaries) runs the language work; Voyage provides embeddings for similar-incident search.

## Docs

- Vision and architecture: [`HANDOFF.md`](HANDOFF.md)
- Milestone handoffs: [`HANDOFF-M2.md`](HANDOFF-M2.md), [`HANDOFF-M3.md`](HANDOFF-M3.md), [`HANDOFF-M4.md`](HANDOFF-M4.md)
- Service runbook: [`docs/pipeline.md`](docs/pipeline.md)
- Postmortem generator: [`docs/postmortems.md`](docs/postmortems.md)
- The one write permission ask: [`docs/github-app.md`](docs/github-app.md)
- Product spec: [`.claude/prds/culprit.prd.md`](.claude/prds/culprit.prd.md)
