"""Corpus invariants over recorded run records (plan Task 9).

These run over whatever is in ``runs/`` — an empty corpus passes trivially, so
the suite is green before recording and enforces the anti-leakage / labeling
rules once the corpus exists. SHA-resolvability against the fork is gated behind
network + clone availability so CI (which has neither) skips it.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import subprocess

import pytest

from harness.config import FIXTURES_DIR, REPO_ROOT, TCF_WORK_DIR
from harness.runrecord import load_all_run_records

RUNS = load_all_run_records()
CODE_RUNS = [r for r in RUNS if r.fault_class == "code"]
ABSTAIN_RUNS = [r for r in RUNS if r.ground_truth == "abstain"]
BASELINE_RUNS = [r for r in RUNS if r.ground_truth == "no_incident"]

GITHUB_DEPLOY_DIR = FIXTURES_DIR / "github" / "workflow_run"
SNS_DIR = FIXTURES_DIR / "sns"
SNS_SIGNING_CERT = REPO_ROOT / "harness" / "snsfeed_inputs" / "sns_signing_cert.pem"

# fault_ids that SHOULD carry an SNS fixture (silent faults + infra dedup cases).
SNS_FAULT_IDS = {
    "n-plus-one-section-instructor-prefetch",
    "cartesian-join-gpa-annotation-timeout",
    "bad-migration-drop-trigram-gin-indexes",
    "search-silent-zero-results",
    "gunicorn-worker-oom",
    "redis-down",
    "db-stopped",
}


def _load_deploy(run):
    """Load a run's deploy fixture -> (envelope, decoded workflow_run payload)."""
    assert run.deploy, f"{run.run_id}: no deploy fixture linked"
    path = REPO_ROOT / run.deploy
    assert path.exists(), f"{run.run_id}: deploy fixture missing at {run.deploy}"
    env = json.loads(path.read_text())
    payload = json.loads(env["raw_body"].encode("latin-1"))
    return env, payload


def test_code_runs_culprit_in_window_never_release():
    """The core anti-leakage invariant: culprit is a window commit, and for
    multi-commit windows it is never the release (window head)."""
    for r in CODE_RUNS:
        assert r.culprit_sha, f"{r.run_id}: code run missing culprit_sha"
        assert r.culprit_in_window(), f"{r.run_id}: culprit not in recorded window"
        if len(r.window) > 1:
            assert not r.culprit_is_release(), (
                f"{r.run_id}: culprit == release SHA (label leakage)"
            )


def test_multicommit_culprit_off_head_majority():
    """In >=1/3 of multi-commit code cases the culprit is not the head."""
    multi = [r for r in CODE_RUNS if len(r.window) > 1]
    if not multi:
        pytest.skip("no multi-commit code runs recorded yet")
    off_head = sum(1 for r in multi if not r.culprit_is_release())
    assert off_head >= len(multi) / 3


def test_abstain_runs_have_no_culprit_but_carry_release():
    for r in ABSTAIN_RUNS:
        assert r.culprit_sha is None, f"{r.run_id}: abstain run must have no culprit"
        assert r.release_sha, f"{r.run_id}: abstain run must carry a (benign) release"
        assert not any(c.is_culprit for c in r.window)


def test_baseline_runs_have_no_incident():
    for r in BASELINE_RUNS:
        assert r.culprit_sha is None
        assert not any(c.is_culprit for c in r.window)


def test_run_ids_unique():
    ids = [r.run_id for r in RUNS]
    assert len(ids) == len(set(ids))


def test_no_orphaned_sentry_fixtures():
    """Every recorded Sentry fixture is referenced by exactly one run record —
    no strays from late-arriving webhooks or ad-hoc diagnostics."""
    sentry_dir = FIXTURES_DIR / "sentry"
    if not sentry_dir.exists():
        return
    referenced = {p for r in RUNS for p in r.fixture_paths}
    repo_root = FIXTURES_DIR.parent
    for f in sentry_dir.rglob("*.json"):
        rel = str(f.relative_to(repo_root))
        assert rel in referenced, f"orphaned fixture (no run references it): {rel}"


def _fork_has(sha: str) -> bool:
    out = subprocess.run(
        ["git", "cat-file", "-t", sha],
        cwd=TCF_WORK_DIR,
        capture_output=True,
        text=True,
    )
    return out.returncode == 0 and out.stdout.strip() == "commit"


def test_recorded_webhook_signatures_verify():
    """Every recorded Sentry webhook's HMAC signature verifies against the
    integration Client Secret (the secret is env-injected, never committed)."""
    secret = os.environ.get("SENTRY_CLIENT_SECRET")
    if not secret:
        pytest.skip("SENTRY_CLIENT_SECRET not set")
    sentry_dir = FIXTURES_DIR / "sentry"
    fixtures = list(sentry_dir.rglob("*.json")) if sentry_dir.exists() else []
    if not fixtures:
        pytest.skip("no recorded sentry fixtures yet")
    checked = 0
    for f in fixtures:
        env = json.loads(f.read_text())
        sig = env["headers"].get("sentry-hook-signature")
        if not sig:
            continue
        raw = env["raw_body"].encode("latin-1")
        expected = hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
        assert hmac.compare_digest(expected, sig), f"{f.name}: bad signature"
        checked += 1
    if checked == 0:
        pytest.skip("no signed fixtures")


# --- GitHub deploy-feed (workflow_run) invariants (plan Task 8) -------------


def test_every_run_has_a_deploy_fixture():
    """Every recorded run shipped a deploy; each links to a workflow_run fixture
    in the recorder envelope format (the M2 deploy-feed ingest contract)."""
    assert RUNS, "no runs recorded yet"
    for r in RUNS:
        env, _ = _load_deploy(r)
        assert env["source"] == "github"
        assert env["resource"] == "workflow_run"
        assert env["headers"].get("x-github-event") == "workflow_run"


def test_deploy_head_sha_is_the_release_sha():
    """The deploy shipped the window head — so head_sha == release_sha, and it is
    resolvable on the fork (asserted separately)."""
    for r in RUNS:
        _, p = _load_deploy(r)
        wr = p["workflow_run"]
        assert wr["head_sha"] == r.release_sha, (
            f"{r.run_id}: deploy head_sha {wr['head_sha'][:10]} != release "
            f"{r.release_sha[:10]}"
        )
        assert wr["head_commit"]["id"] == r.release_sha


def test_deploy_event_is_workflow_run():
    """Fidelity: a real 'AWS Deployment' is chained off CI, so the deploy's own
    event is 'workflow_run' (not push/workflow_dispatch)."""
    for r in RUNS:
        _, p = _load_deploy(r)
        assert p["workflow_run"]["event"] == "workflow_run", r.run_id


def test_deploy_feed_never_names_the_culprit():
    """Anti-leakage: the deploy feed must not expose the fault id/culprit — a
    consumer that read the answer off head_branch would be cheating."""
    for r in RUNS:
        _, p = _load_deploy(r)
        head_branch = p["workflow_run"]["head_branch"]
        assert r.fault_id not in head_branch, (
            f"{r.run_id}: fault id leaks into deploy head_branch={head_branch!r}"
        )


def test_deploy_payload_has_required_timeline_fields():
    """The deploy-timeline schema M2 keys on must be present."""
    required = {
        "head_sha",
        "head_branch",
        "event",
        "status",
        "conclusion",
        "run_started_at",
        "updated_at",
        "created_at",
    }
    for r in RUNS:
        _, p = _load_deploy(r)
        assert p["action"], r.run_id
        wr = p["workflow_run"]
        assert not (required - set(wr)), f"{r.run_id}: missing {required - set(wr)}"
        assert p["workflow"]["name"] == "AWS Deployment"


def test_deploy_webhook_signatures_verify():
    """Every deploy fixture's `x-hub-signature-256` verifies against the fork's
    real GitHub webhook secret (env-injected as CULPRIT_GH_WEBHOOK_SECRET, never
    committed) — the GitHub parallel of the Sentry signature invariant."""
    secret = os.environ.get("CULPRIT_GH_WEBHOOK_SECRET")
    if not secret:
        pytest.skip("CULPRIT_GH_WEBHOOK_SECRET not set")
    if not GITHUB_DEPLOY_DIR.exists():
        pytest.skip("no deploy fixtures yet")
    checked = 0
    for f in GITHUB_DEPLOY_DIR.rglob("*.json"):
        env = json.loads(f.read_text())
        sig = env["headers"].get("x-hub-signature-256")
        if not sig:
            continue
        raw = env["raw_body"].encode("latin-1")
        expected = (
            "sha256=" + hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
        )
        assert hmac.compare_digest(expected, sig), f"{f.name}: bad signature"
        checked += 1
    if checked == 0:
        pytest.skip("no signed deploy fixtures")


def test_deploy_fixtures_have_no_personal_emails():
    """Corpus posture (handoff §6): no non-redacted personal emails in the deploy
    feed. ``git@github.com`` (ssh_url) and ``*@users.noreply.github.com`` are
    structural GitHub addresses, not PII."""
    if not GITHUB_DEPLOY_DIR.exists():
        return
    email_re = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
    allowed = {"redacted@example.com", "git@github.com"}
    for f in GITHUB_DEPLOY_DIR.rglob("*.json"):
        for m in email_re.findall(f.read_text()):
            assert m in allowed or m.endswith("@users.noreply.github.com"), (
                f"{f.name}: non-redacted personal email {m!r}"
            )


def test_no_orphaned_github_fixtures():
    """Every deploy fixture is referenced by exactly one run (mirrors the Sentry
    orphan invariant)."""
    if not GITHUB_DEPLOY_DIR.exists():
        return
    referenced = {r.deploy for r in RUNS if r.deploy}
    for f in GITHUB_DEPLOY_DIR.rglob("*.json"):
        rel = str(f.relative_to(REPO_ROOT))
        assert rel in referenced, (
            f"orphaned deploy fixture (no run references it): {rel}"
        )


# --- synthesized SNS/CloudWatch alarm-feed invariants (plan Task 6) ----------


def _load_sns(run):
    """Load a run's SNS fixture -> (envelope, decoded SNS Notification)."""
    assert run.sns, f"{run.run_id}: no SNS fixture linked"
    path = REPO_ROOT / run.sns
    assert path.exists(), f"{run.run_id}: SNS fixture missing at {run.sns}"
    env = json.loads(path.read_text())
    notification = json.loads(env["raw_body"].encode("latin-1"))
    return env, notification


def test_sns_fixtures_link_the_expected_silent_and_infra_runs():
    """Exactly the silent faults + infra dedup cases carry an SNS fixture."""
    linked = {r.fault_id for r in RUNS if r.sns}
    assert linked == SNS_FAULT_IDS, f"unexpected SNS-linked faults: {linked}"


def test_no_orphaned_sns_fixtures():
    """Every SNS fixture is referenced by exactly one run (mirrors Sentry/GitHub)."""
    if not SNS_DIR.exists():
        return
    referenced = {r.sns for r in RUNS if r.sns}
    for f in SNS_DIR.rglob("*.json"):
        rel = str(f.relative_to(REPO_ROOT))
        assert rel in referenced, f"orphaned SNS fixture (no run references it): {rel}"


def test_sns_envelope_is_reconstructed_and_text_plain():
    """The classic SNS gotcha: JSON body delivered as text/plain; the route must
    dispatch on the x-amz-sns-message-type header, not the content type."""
    for r in RUNS:
        if not r.sns:
            continue
        env, _ = _load_sns(r)
        assert env["source"] == "sns" and env["resource"] == "notification"
        assert env.get("reconstructed") is True
        assert env["headers"]["content-type"].startswith("text/plain")
        assert env["headers"]["x-amz-sns-message-type"] == "Notification"


def test_sns_payloads_never_name_the_fault():
    """Anti-leakage: alarm names/topics are generic infra metrics — a consumer
    that read the fault off the SNS payload would be cheating."""
    for r in RUNS:
        if not r.sns:
            continue
        env, notif = _load_sns(r)
        assert r.fault_id not in json.dumps(env), (
            f"{r.run_id}: fault id leaks into its SNS payload"
        )
        message = json.loads(notif["Message"])
        assert r.fault_id not in message["AlarmName"]
        assert message["NewStateValue"] == "ALARM"


def test_sns_signatures_verify_against_the_vendored_cert():
    """Every SNS fixture's Signature verifies against the vendored signing cert
    (committed) — so the real verification code path runs offline, no secret
    needed (unlike the HMAC feeds). The private key is gitignored."""
    from harness import snsfeed

    if not SNS_DIR.exists() or not SNS_SIGNING_CERT.exists():
        pytest.skip("no SNS fixtures / vendored cert")
    cert_pem = SNS_SIGNING_CERT.read_bytes()
    checked = 0
    for r in RUNS:
        if not r.sns:
            continue
        _, notif = _load_sns(r)
        if not notif.get("Signature"):
            continue
        assert snsfeed.verify_signature(notif, cert_pem), (
            f"{r.run_id}: SNS signature does not verify"
        )
        checked += 1
    assert checked > 0, "no signed SNS fixtures verified"


@pytest.mark.skipif(not TCF_WORK_DIR.exists(), reason="no working clone (CI)")
def test_every_recorded_sha_is_resolvable():
    """Recorded window SHAs must stay fetchable (M2 reads diffs/blame at them)."""
    if not RUNS:
        pytest.skip("no runs recorded yet")
    for r in RUNS:
        for c in r.window:
            assert _fork_has(c.sha), (
                f"{r.run_id}: window SHA {c.sha[:10]} not resolvable"
            )
