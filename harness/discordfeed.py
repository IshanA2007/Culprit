"""Postmortem-input fixtures — the rollback deploy + the Discord chat thread (M4).

M4 needs two synthesized inputs per resolved incident, reusing the 22-run corpus:

* a **fix-deploy** — a rollback "AWS Deployment" ``workflow_run`` shipping
  ``base_sha`` (the last-known-good, a real fork commit) after the fault shipped —
  the fixing commit the postmortem cites in its timeline. Only **code** faults get
  one; infra faults are fixed by remediation (no code shipped), so they carry none
  and ``fixing_sha`` stays NULL (honest — the fix-side parallel to abstention).
* a **Discord thread** — the incident channel's on-call chatter, the human
  narrative the postmortem joins to the machine timeline. Every incident-producing
  run gets one; the benign baseline (no incident) does not.

Deployfeed-grade provenance (mirrors ``harness/deployfeed.py`` /
``harness/snsfeed.py``): real schema (the fix-deploy is a genuine ``workflow_run``,
the thread mirrors Discord's message shape — ``discord_inputs/template_message.json``),
deterministic synthesized ids/timestamps (byte-stable regeneration), stamped
``"reconstructed": true``, and **no fault identity in any field** — the thread is
generic on-call text that never names the fault or the culprit sha (anti-leakage,
the ``head_branch: master`` precedent).
"""

from __future__ import annotations

import hashlib
import hmac
import json
from pathlib import Path

from harness.config import FIXTURES_DIR, REPO_ROOT
from harness.deployfeed import (
    DEPLOY_BRANCH,
    DEPLOY_DURATION_SECONDS,
    DEPLOY_WORKFLOW_NAME,
    DEPLOY_WORKFLOW_PATH,
    WORKFLOW_ID,
    DeployInputs,
    _delivery_uuid,
    _node_id,
    _plus_seconds,
    _seed_int,
    build_workflow_object,
    iso_from_compact,
    load_inputs,
)
from harness.runrecord import RunRecord, load_all_run_records

# The rollback ships this long after the fault was injected (documented delay —
# time to detect + decide + roll back). Deterministic; regeneration is stable.
FIX_DELAY_SECONDS = 900

DISCORD_FIXTURE_DIR = FIXTURES_DIR / "discord"
FIX_DEPLOY_DIR = FIXTURES_DIR / "github" / "workflow_run"

# Generic on-call chatter — the human narrative. Deliberately fault-agnostic: it
# never names the fault, the module, or the culprit sha (anti-leakage). (author,
# text) pairs; timestamps are synthesized relative to the incident open.
THREAD_SCRIPT = [
    ("on-call", "Heads up — the site is throwing errors, taking a look now."),
    (
        "culprit-bot",
        "Posted a brief: likely culprit is the last deploy. Runbook offered.",
    ),
    ("on-call", "Rolling back the last deploy per the runbook."),
    ("maintainer", "Rollback shipped — error rate is dropping."),
    ("maintainer", "Green now. Resolving the incident."),
]


# --- fix-deploy (a rollback workflow_run shipping base_sha) ------------------


def build_fix_deploy_payload(run: RunRecord, inputs: DeployInputs) -> dict:
    """A rollback ``workflow_run`` webhook body — ships ``base_sha`` as the fix."""
    full_name = inputs.fork_repo["full_name"]
    api = f"https://api.github.com/repos/{full_name}"
    run_id = _seed_int(run.run_id, "fix_run_id", 29_000_000_000, 30_000_000_000)
    started = _plus_seconds(iso_from_compact(run.injected_at), FIX_DELAY_SECONDS)
    completed = _plus_seconds(started, DEPLOY_DURATION_SECONDS)

    workflow_run = {
        "id": run_id,
        "name": DEPLOY_WORKFLOW_NAME,
        "node_id": _node_id("WFR", run.run_id, "fix_node"),
        "head_branch": DEPLOY_BRANCH,
        "head_sha": run.base_sha,  # rollback to last-known-good (a real fork commit)
        "path": DEPLOY_WORKFLOW_PATH,
        "display_title": DEPLOY_WORKFLOW_NAME,
        "event": "workflow_run",
        "status": "completed",
        "conclusion": "success",
        "workflow_id": WORKFLOW_ID,
        "url": f"{api}/actions/runs/{run_id}",
        "html_url": f"https://github.com/{full_name}/actions/runs/{run_id}",
        "created_at": started,
        "updated_at": completed,
        "run_started_at": started,
        "actor": inputs.owner,
        "triggering_actor": inputs.owner,
        "run_attempt": 1,
        # The rollback's head_commit IS base_sha (no PII; a generic rollback note).
        "head_commit": {
            "id": run.base_sha,
            "message": f"Roll back to last-known-good {run.base_sha[:8]}",
            "timestamp": started,
            "author": {"name": "tCF Deploy", "email": "redacted@example.com"},
            "committer": {"name": "tCF Deploy", "email": "redacted@example.com"},
        },
        "repository": inputs.fork_repo,
        "head_repository": inputs.fork_repo,
    }
    return {
        "action": "completed",
        "workflow_run": workflow_run,
        "workflow": build_workflow_object(inputs),
        "repository": inputs.fork_repo,
        "sender": inputs.owner,
    }


def build_fix_deploy_envelope(
    run: RunRecord, inputs: DeployInputs, *, secret: str | None = None
) -> dict:
    """Wrap the rollback payload in the recorder envelope (the ingest shape)."""
    payload = build_fix_deploy_payload(run, inputs)
    body_bytes = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode(
        "utf-8"
    )
    raw_body = body_bytes.decode("latin-1")
    completed = payload["workflow_run"]["updated_at"]

    headers = {
        "content-type": "application/json",
        "user-agent": "GitHub-Hookshot/culprit-harness",
        "x-github-event": "workflow_run",
        "x-github-delivery": _delivery_uuid(run.run_id + ":fix"),
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
        "reconstructed": True,
    }


# --- Discord chat thread (the human narrative) ------------------------------


def build_thread_fixture(run: RunRecord) -> dict:
    """A generic on-call thread for the incident channel (fault-agnostic)."""
    channel_id = str(_seed_int(run.run_id, "channel", 10**18, 11 * 10**17))
    base_ts = iso_from_compact(run.injected_at)
    messages = []
    for i, (author, content) in enumerate(THREAD_SCRIPT):
        messages.append(
            {
                "id": str(_seed_int(run.run_id, f"msg{i}", 10**18, 11 * 10**17)),
                "channel_id": channel_id,
                "author": {
                    "id": str(_seed_int(run.run_id, f"author{author}", 10**17, 10**18)),
                    "username": author,
                    "global_name": author.replace("-", " ").title(),
                },
                "content": content,
                "timestamp": _plus_seconds(base_ts, 120 * (i + 1)) + ".000000+00:00",
            }
        )
    return {
        "source": "discord",
        "resource": "channel_messages",
        "channel_id": channel_id,
        "messages": messages,
        "reconstructed": True,
    }


# --- fixture naming + backfill ----------------------------------------------


def _fix_deploy_name(run: RunRecord) -> str:
    suffix = hashlib.sha256(f"{run.run_id}|fix".encode()).hexdigest()[:8]
    return f"{run.injected_at}-fix-{suffix}.json"


def _thread_name(run: RunRecord) -> str:
    suffix = hashlib.sha256(f"{run.run_id}|thread".encode()).hexdigest()[:8]
    return f"{run.injected_at}-{suffix}.json"


def backfill_postmortem_inputs(
    *,
    secret: str | None = None,
    inputs_dir: Path | None = None,
    runs_dir: Path | None = None,
) -> dict:
    """Generate the fix-deploy + thread fixtures and link them in the run records.

    Deterministic + idempotent. Code faults get a fix-deploy (rollback to
    base_sha); every incident-producing run gets a thread; the benign baseline
    (ground_truth ``no_incident``) gets neither.
    """
    inputs, _ = load_inputs(inputs_dir)
    runs = load_all_run_records(runs_dir)
    DISCORD_FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
    FIX_DEPLOY_DIR.mkdir(parents=True, exist_ok=True)

    fix_written = thread_written = 0
    for run in runs:
        if run.ground_truth == "no_incident":
            continue  # the baseline produces no incident -> no postmortem inputs

        thread = build_thread_fixture(run)
        thread_path = DISCORD_FIXTURE_DIR / _thread_name(run)
        thread_path.write_text(json.dumps(thread, indent=2))
        run.thread = str(thread_path.relative_to(REPO_ROOT))
        thread_written += 1

        if run.fault_class == "code":
            env = build_fix_deploy_envelope(run, inputs, secret=secret)
            fix_path = FIX_DEPLOY_DIR / _fix_deploy_name(run)
            fix_path.write_text(json.dumps(env, indent=2))
            run.fix_deploy = str(fix_path.relative_to(REPO_ROOT))
            fix_written += 1

        run.write(runs_dir)

    return {
        "runs": len(runs),
        "fix_deploys_written": fix_written,
        "threads_written": thread_written,
    }
