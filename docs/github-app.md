# Culprit — GitHub App access ask (the ONE write permission)

Culprit reads theCourseForum2 with an unauthenticated/public token (diffs, blame,
files at a pinned SHA — see [`docs/pipeline.md`](pipeline.md)). Milestone 4 adds
**exactly one write capability**: drafting a **postmortem pull request** after an
incident resolves. This is the *only* thing Culprit ever writes, and it **never
merges** — a human reviews and merges the PR.

This doc is the hand-to-maintainer artifact (the analog of the read-only IAM ask
in [`docs/aws/aws-access.md`](aws/aws-access.md)).

## What to grant

Register a **GitHub App** (owned by the maintainer / the tCF org) and install it on
**`theCourseForum2` only**, with the minimal permission set:

| Permission | Level | Why |
|---|---|---|
| **Contents** | **Read & write** | Create the `culprit/postmortem-*` branch and write `postmortems/YYYY-MM-DD-slug.md` on it |
| **Pull requests** | **Read & write** | Open the postmortem PR into the default branch |
| *(everything else)* | **No access** | Culprit needs nothing else — no Actions, no admin, no merge-implying scope |

**No merge permission is required or used.** Merging a PR is done through the normal
`Contents: write` a human already has; Culprit's code path contains **no merge
call** (`culprit/github_app.py` — verified by `tests/test_github_app.py`).

## Why an App (not a PAT)

- **Scoped to one repo.** An installation token is limited to `theCourseForum2` and
  the two permissions above — it cannot touch other repos or other scopes.
- **Short-lived tokens.** Culprit mints a ~10-minute App JWT (RS256) and exchanges
  it for a short-lived installation token per write; nothing long-lived is stored.
- **Auditable + revocable.** The maintainer sees the App, its permissions, and its
  activity, and can uninstall it in one click.

## Configuration (Culprit side — all inert by default)

Set these in `.env` (gitignored). **Absent → the writer is inert and Culprit stays
in dry-run** (renders the Markdown + the PR request, pushes nothing):

| Var | Meaning |
|---|---|
| `GITHUB_APP_ID` | the App's numeric id |
| `GITHUB_APP_PRIVATE_KEY` **or** `GITHUB_APP_PRIVATE_KEY_PATH` | the App's RSA private key (PEM contents, or a path to it — gitignored) |
| `GITHUB_APP_INSTALLATION_ID` | the installation id on `theCourseForum2` |
| `POSTMORTEMS_REPO` | target repo (defaults to `GITHUB_REPO`, the fork) |
| `POSTMORTEMS_BASE_BRANCH` | the PR base (default `master`) |
| `POSTMORTEM_DRY_RUN` | `true` (default) renders without pushing; set `false` **and** configure the App to open real PRs |

## The offer-only guarantee (permanent)

- Culprit **drafts** a Markdown PR; a human **merges** it. It never auto-merges and
  never publishes unilaterally.
- The write is idempotent — **one PR per incident** (unique `incident_id` in the
  `postmortems` table); a re-draft never opens a second PR.
- The draft is built from **persisted incident data only** — never from any
  ground-truth label (anti-leakage; `culprit/eval/score.py` stays the only
  ground-truth reader).
