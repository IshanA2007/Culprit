"""GitHub deploy-feed generator — the ``workflow_run`` half of the ingest contract.

Every recorded scenario shipped a deploy: the release task (decision 7) that
built the window and set the Sentry release at ``release_sha``. In production
that deploy is theCourseForum2's **"AWS Deployment"** GitHub Actions workflow,
and GitHub emits a ``workflow_run`` webhook describing it. That payload is the
deploy-timeline half of Milestone 2's ingest contract — the mirror image of the
Sentry ``event_alert`` / ``issue`` fixtures — and it was the one piece of M1
Task 8 left deferred (`fixtures/github/workflow_run/` held only a ``.gitkeep``).

This module reconstructs those payloads **shape-faithfully from real data**:

* the *schema* is a real production "AWS Deployment" run captured from the
  upstream repo (``deployfeed_inputs/template_upstream.json``) — every field
  name/type matches, enforced by ``test_deployfeed.py``;
* the *substantive* fields are real: ``head_sha`` is the run's real
  ``release_sha`` (resolvable on the fork), ``head_commit`` is that commit's real
  metadata, ``repository``/``sender`` are the real fork + owner objects;
* only identifiers with no external referent (run id, node ids, delivery uuid)
  are synthesized — deterministically, so regeneration is byte-stable.

Fidelity notes (also in ``docs/harness.md`` and the fixtures' ``PROVENANCE.md``):

* ``event == "workflow_run"`` — the real "AWS Deployment" is *chained off CI*
  (``on: workflow_run: workflows: ["Continuous Integration"]``), so a genuine
  deploy's own event is ``workflow_run``, not ``push``. Reconstructing from the
  real upstream run preserves this; a fork-triggered ``fake-deploy.yml`` fired by
  ``push``/``workflow_dispatch`` would *not*.
* ``head_branch`` is normalized to the production deploy branch (``master``) and
  deliberately **not** the internal ``fault/<id>-<ts>`` branch — the deploy feed
  must never name the culprit (anti-leakage, same spirit as the run-record
  window rules).
* ``conclusion == "success"``: the deploy itself succeeds; the injected fault is
  a code commit *within* the shipped window, not a failed deploy.
* The payload *content* is reconstructed (stamped ``"reconstructed": true``), not a
  raw GitHub delivery — but the committed fixtures ARE signed with a **genuine**
  GitHub webhook secret (``CULPRIT_GH_WEBHOOK_SECRET``, env-injected, never
  committed). That secret was configured on a live ``workflow_run`` webhook on the
  fork and GitHub was confirmed delivering genuine events signed with it (the
  temporary webhook was then torn down). So these ``x-hub-signature-256`` headers
  verify exactly as a real delivery would (parity with the Sentry fixtures
  verifying against ``SENTRY_CLIENT_SECRET``, and with ``scrub.py``'s re-sign idiom).

The generator is offline + deterministic (reads only vendored inputs). Signing is
deterministic too, so ``culprit-harness backfill-deploys`` regenerates byte-stable
fixtures — but reproducing the *signed* corpus needs ``CULPRIT_GH_WEBHOOK_SECRET``
in the environment (as the Sentry side needs ``SENTRY_CLIENT_SECRET``).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from harness.config import FIXTURES_DIR, REPO_ROOT
from harness.runrecord import RunRecord, load_all_run_records

# The fork's deploy workflow (added as .github/workflows/fake-deploy.yml, named
# "AWS Deployment" — collision-free once the fork's own aws.yml is disabled).
DEPLOY_WORKFLOW_NAME = "AWS Deployment"
DEPLOY_WORKFLOW_PATH = ".github/workflows/fake-deploy.yml"
# Normalized to the production deploy branch (non-leaking; see module docstring).
DEPLOY_BRANCH = "master"
# Synthesized workflow id (fake-deploy.yml is not registered on the fork, so it
# has no real Actions id); deterministic + documented.
WORKFLOW_ID = 900_000_001
# Real "AWS Deployment" wall-clock, from the vendored upstream template
# (updated_at 2026-07-02T01:51:08Z - run_started_at 2026-07-02T01:42:56Z).
DEPLOY_DURATION_SECONDS = 492

INPUTS_DIR = REPO_ROOT / "harness" / "deployfeed_inputs"
DEPLOY_FIXTURE_DIR = FIXTURES_DIR / "github" / "workflow_run"


@dataclass
class DeployInputs:
    """Real GitHub objects the reconstructed payload embeds verbatim."""

    fork_repo: dict  # GET /repos/<fork>
    owner: dict  # GET /users/<owner>


# --- deterministic synthesis (no Date.now / random — regeneration is stable) --


def _digest(*parts: str) -> bytes:
    return hashlib.sha256("|".join(parts).encode()).digest()


def _seed_int(run_id: str, salt: str, lo: int, hi: int) -> int:
    n = int.from_bytes(_digest(run_id, salt)[:8], "big")
    return lo + (n % (hi - lo))


def _node_id(prefix: str, run_id: str, salt: str) -> str:
    raw = base64.urlsafe_b64encode(_digest(run_id, salt)[:12]).decode().rstrip("=")
    return f"{prefix}_{raw}"


def _delivery_uuid(run_id: str) -> str:
    return str(uuid.UUID(bytes=_digest(run_id, "delivery")[:16]))


REDACTED_EMAIL = "redacted@example.com"
# Non-PII, structural addresses that must survive redaction (e.g. the fork's
# ssh_url is "git@github.com:owner/repo.git"; noreply is GitHub's own).
_KEEP_EMAILS = {"git@github.com"}


def _safe_email(value: str | None) -> str | None:
    """Redact a personal email, preserving structural/noreply addresses.

    Keeps the corpus posture (handoff §6: 0 non-redacted personal emails in the
    pushed tree) without corrupting real GitHub URL fields."""
    if not value or "@" not in value:
        return value
    if value in _KEEP_EMAILS or value.endswith("@users.noreply.github.com"):
        return value
    return REDACTED_EMAIL


def _redact_identity(identity: dict) -> dict:
    """Redact the email of a commit author/committer identity, keep the name."""
    out = dict(identity)
    if "email" in out:
        out["email"] = _safe_email(out["email"])
    return out


def iso_from_compact(ts: str) -> str:
    """``'20260704T034625'`` -> ``'2026-07-04T03:46:25Z'`` (the run's injected_at
    is when the release task deployed the window)."""
    return datetime.strptime(ts, "%Y%m%dT%H%M%S").strftime("%Y-%m-%dT%H:%M:%SZ")


def _plus_seconds(iso: str, seconds: int) -> str:
    dt = datetime.strptime(iso, "%Y-%m-%dT%H:%M:%SZ") + timedelta(seconds=seconds)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


# --- payload assembly -------------------------------------------------------


def build_workflow_run(
    run: RunRecord, release_commit: dict, inputs: DeployInputs
) -> dict:
    """The ``workflow_run`` object — same field set as a real production run."""
    full_name = inputs.fork_repo["full_name"]
    api = f"https://api.github.com/repos/{full_name}"
    run_id = _seed_int(run.run_id, "run_id", 28_000_000_000, 29_000_000_000)
    check_suite_id = _seed_int(
        run.run_id, "check_suite", 77_000_000_000, 78_000_000_000
    )
    started = iso_from_compact(run.injected_at)
    completed = _plus_seconds(started, DEPLOY_DURATION_SECONDS)

    head_commit = {
        "id": run.release_sha,
        "tree_id": release_commit["tree_id"],
        "message": release_commit["message"],
        "timestamp": release_commit["timestamp"],
        # Defensive PII redaction: commit-author emails never leak into fixtures.
        "author": _redact_identity(release_commit["author"]),
        "committer": _redact_identity(release_commit["committer"]),
    }

    return {
        "id": run_id,
        "name": DEPLOY_WORKFLOW_NAME,
        "node_id": _node_id("WFR", run.run_id, "node"),
        "head_branch": DEPLOY_BRANCH,
        "head_sha": run.release_sha,
        "path": DEPLOY_WORKFLOW_PATH,
        "display_title": DEPLOY_WORKFLOW_NAME,
        "run_number": _seed_int(run.run_id, "run_number", 100, 1000),
        "event": "workflow_run",
        "status": "completed",
        "conclusion": "success",
        "workflow_id": WORKFLOW_ID,
        "check_suite_id": check_suite_id,
        "check_suite_node_id": _node_id("CS", run.run_id, "check_suite_node"),
        "url": f"{api}/actions/runs/{run_id}",
        "html_url": f"https://github.com/{full_name}/actions/runs/{run_id}",
        "pull_requests": [],
        "created_at": started,
        "updated_at": completed,
        "run_started_at": started,
        "actor": inputs.owner,
        "triggering_actor": inputs.owner,
        "run_attempt": 1,
        "referenced_workflows": [],
        "jobs_url": f"{api}/actions/runs/{run_id}/jobs",
        "logs_url": f"{api}/actions/runs/{run_id}/logs",
        "check_suite_url": f"{api}/check-suites/{check_suite_id}",
        "artifacts_url": f"{api}/actions/runs/{run_id}/artifacts",
        "cancel_url": f"{api}/actions/runs/{run_id}/cancel",
        "rerun_url": f"{api}/actions/runs/{run_id}/rerun",
        "previous_attempt_url": None,
        "workflow_url": f"{api}/actions/workflows/{WORKFLOW_ID}",
        "head_commit": head_commit,
        "repository": inputs.fork_repo,
        "head_repository": inputs.fork_repo,
    }


def build_workflow_object(inputs: DeployInputs) -> dict:
    """The ``workflow`` key of the webhook payload (the deploy workflow itself)."""
    full_name = inputs.fork_repo["full_name"]
    api = f"https://api.github.com/repos/{full_name}"
    return {
        "id": WORKFLOW_ID,
        "node_id": _node_id("W", full_name, "workflow_node"),
        "name": DEPLOY_WORKFLOW_NAME,
        "path": DEPLOY_WORKFLOW_PATH,
        "state": "active",
        "created_at": "2026-07-04T00:00:00Z",
        "updated_at": "2026-07-04T00:00:00Z",
        "url": f"{api}/actions/workflows/{WORKFLOW_ID}",
        "html_url": f"https://github.com/{full_name}/blob/{DEPLOY_BRANCH}/{DEPLOY_WORKFLOW_PATH}",
        "badge_url": f"https://github.com/{full_name}/workflows/{DEPLOY_WORKFLOW_NAME.replace(' ', '%20')}/badge.svg",
    }


def build_webhook_payload(
    run: RunRecord, release_commit: dict, inputs: DeployInputs
) -> dict:
    """The full ``workflow_run`` webhook body: action + the four object keys.

    No ``organization`` key: the fork is owned by a user, not an org (the real
    upstream org payload carries one; a personal fork does not)."""
    return {
        "action": "completed",
        "workflow_run": build_workflow_run(run, release_commit, inputs),
        "workflow": build_workflow_object(inputs),
        "repository": inputs.fork_repo,
        "sender": inputs.owner,
    }


def deploy_fixture_name(run: RunRecord) -> str:
    """Deterministic ``<injected_at>-<8hex>.json`` (mirrors the recorder naming)."""
    suffix = _digest(run.run_id, "fixture").hex()[:8]
    return f"{run.injected_at}-{suffix}.json"


def build_envelope(
    run: RunRecord,
    release_commit: dict,
    inputs: DeployInputs,
    *,
    secret: str | None = None,
) -> dict:
    """Wrap the payload in the recorder envelope (byte-for-byte the ingest shape).

    ``raw_body`` is the UTF-8 wire bytes stored via latin-1 (1:1 byte<->codepoint),
    exactly as ``recorder/app.py`` records live webhooks."""
    payload = build_webhook_payload(run, release_commit, inputs)
    body_bytes = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode(
        "utf-8"
    )
    raw_body = body_bytes.decode("latin-1")
    completed = build_workflow_run(run, release_commit, inputs)["updated_at"]

    headers = {
        "content-type": "application/json",
        "user-agent": "GitHub-Hookshot/culprit-harness",
        "x-github-event": "workflow_run",
        "x-github-delivery": _delivery_uuid(run.run_id),
        "x-github-hook-id": str(
            _seed_int(run.run_id, "hook", 400_000_000, 500_000_000)
        ),
        "x-github-hook-installation-target-id": str(inputs.fork_repo["id"]),
        "x-github-hook-installation-target-type": "repository",
    }
    if secret:
        digest = hmac.new(secret.encode(), body_bytes, hashlib.sha256).hexdigest()
        headers["x-hub-signature-256"] = f"sha256={digest}"

    return {
        "received_at": completed,
        "source": "github",
        "resource": "workflow_run",
        "headers": headers,
        "raw_body": raw_body,
        # Honest provenance stamp: assembled from real objects, not GitHub-delivered.
        "reconstructed": True,
    }


# --- vendored real inputs (offline, deterministic) --------------------------


def load_template(inputs_dir: Path | None = None) -> dict:
    """The real upstream 'AWS Deployment' run used as the schema template."""
    inputs_dir = inputs_dir or INPUTS_DIR
    return json.loads((inputs_dir / "template_upstream.json").read_text())


def load_inputs(inputs_dir: Path | None = None) -> tuple[DeployInputs, dict]:
    """Return (DeployInputs, release_commits-by-sha) from the vendored real data."""
    inputs_dir = inputs_dir or INPUTS_DIR
    fork_repo = json.loads((inputs_dir / "fork_repo.json").read_text())
    owner = json.loads((inputs_dir / "owner.json").read_text())
    commits = json.loads((inputs_dir / "release_commits.json").read_text())
    return DeployInputs(fork_repo=fork_repo, owner=owner), commits


def write_deploy_fixture(
    run: RunRecord,
    release_commit: dict,
    inputs: DeployInputs,
    *,
    out_dir: Path | None = None,
    secret: str | None = None,
) -> Path:
    """Write one deploy fixture and return its path (relative-clean for records)."""
    out_dir = out_dir or DEPLOY_FIXTURE_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    env = build_envelope(run, release_commit, inputs, secret=secret)
    dest = out_dir / deploy_fixture_name(run)
    dest.write_text(json.dumps(env, indent=2))
    return dest


def backfill_deploys(
    *,
    secret: str | None = None,
    inputs_dir: Path | None = None,
    out_dir: Path | None = None,
    runs_dir: Path | None = None,
) -> dict:
    """Generate a deploy fixture per recorded run and link it in the run record.

    Deterministic + idempotent: re-running rewrites the same fixture bytes and the
    same ``deploy`` path. Raises if a run's release SHA has no vendored commit
    (regenerate ``deployfeed_inputs/release_commits.json`` — see docs)."""
    inputs, commits = load_inputs(inputs_dir)
    runs = load_all_run_records(runs_dir)
    written = []
    for run in runs:
        release_commit = commits.get(run.release_sha)
        if release_commit is None:
            raise KeyError(
                f"{run.run_id}: no vendored release commit for "
                f"{run.release_sha[:10]} (release_commits.json is stale)"
            )
        dest = write_deploy_fixture(
            run, release_commit, inputs, out_dir=out_dir, secret=secret
        )
        run.deploy = str(dest.relative_to(REPO_ROOT))
        run.write(runs_dir)
        written.append(run.deploy)
    return {"runs": len(runs), "fixtures_written": len(written)}
