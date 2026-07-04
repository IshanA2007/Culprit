"""Unit tests for the GitHub deploy-feed generator (``harness/deployfeed.py``).

The deploy feed reconstructs shape-faithful ``workflow_run`` webhook payloads for
each recorded run's deploy (the release task that shipped ``release_sha``). The
payload *schema* is taken from a real upstream production "AWS Deployment" run;
the substantive fields (``head_sha``, ``head_commit``) are the run's real fork
SHA. These tests pin the generator's public contract.
"""

from __future__ import annotations

import hashlib
import hmac
import json

from harness import deployfeed
from harness.runrecord import RunRecord, WindowCommit

# --- minimal, in-memory inputs (pure functions, no network) -----------------

FORK_REPO = {
    "id": 123,
    "node_id": "R_abc",
    "name": "theCourseForum2",
    "full_name": "IshanA2007/theCourseForum2",
    "private": False,
    "owner": {"login": "IshanA2007", "id": 42, "type": "User"},
    "html_url": "https://github.com/IshanA2007/theCourseForum2",
    "default_branch": "dev",
}

OWNER = {"login": "IshanA2007", "id": 42, "type": "User", "site_admin": False}

RELEASE_COMMIT = {
    "sha": "2126ec08b659479e2231601ccf2683e5a034a222",
    "tree_id": "aaaabbbbccccddddeeee",
    "message": "docs: expand browse view docstring with mode-dispatch details",
    "timestamp": "2026-07-04T03:46:00Z",
    "author": {"name": "Ishan Ajwani", "email": "redacted@example.com"},
    "committer": {"name": "Ishan Ajwani", "email": "redacted@example.com"},
}


def _run(**overrides) -> RunRecord:
    base = dict(
        run_id="20260704T034625-template-noreversematch-instructor-card-w4",
        fault_id="template-noreversematch-instructor-card",
        fault_class="code",
        ground_truth="culprit_commit",
        base_sha="172effabd7f7dc6ccee9f2b69540835eacf77bf0",
        release_sha="2126ec08b659479e2231601ccf2683e5a034a222",
        window=[
            WindowCommit(sha="e0a0" + "0" * 36, message="refactor", is_culprit=True),
            WindowCommit(
                sha="2126ec08b659479e2231601ccf2683e5a034a222",
                message="docs: browse",
                is_decoy=True,
            ),
        ],
        culprit_sha="e0a0" + "0" * 36,
        injected_at="20260704T034625",
    )
    base.update(overrides)
    return RunRecord(**base)


def _inputs() -> deployfeed.DeployInputs:
    return deployfeed.DeployInputs(fork_repo=FORK_REPO, owner=OWNER)


# --- timestamp conversion ---------------------------------------------------


def test_iso_from_compact():
    assert deployfeed.iso_from_compact("20260704T034625") == "2026-07-04T03:46:25Z"


# --- the workflow_run object ------------------------------------------------


def test_workflow_run_head_sha_is_release_sha():
    wr = deployfeed.build_workflow_run(_run(), RELEASE_COMMIT, _inputs())
    assert wr["head_sha"] == _run().release_sha
    assert wr["head_commit"]["id"] == _run().release_sha


def test_workflow_run_event_is_workflow_run():
    """The fidelity invariant: a real 'AWS Deployment' is chained off CI, so its
    own event is 'workflow_run' — not 'push'/'workflow_dispatch'."""
    wr = deployfeed.build_workflow_run(_run(), RELEASE_COMMIT, _inputs())
    assert wr["event"] == "workflow_run"


def test_workflow_run_is_a_successful_completed_deploy():
    wr = deployfeed.build_workflow_run(_run(), RELEASE_COMMIT, _inputs())
    assert wr["status"] == "completed"
    assert wr["conclusion"] == "success"
    assert wr["name"] == deployfeed.DEPLOY_WORKFLOW_NAME


def test_workflow_run_head_branch_is_normalized_and_non_leaking():
    """head_branch must NOT expose the fault id (would leak the culprit into the
    deploy feed); it is normalized to the production deploy branch."""
    wr = deployfeed.build_workflow_run(_run(), RELEASE_COMMIT, _inputs())
    assert wr["head_branch"] == deployfeed.DEPLOY_BRANCH
    assert _run().fault_id not in wr["head_branch"]


def test_workflow_run_timestamps_ordered():
    wr = deployfeed.build_workflow_run(_run(), RELEASE_COMMIT, _inputs())
    assert wr["run_started_at"] == wr["created_at"] == "2026-07-04T03:46:25Z"
    assert wr["updated_at"] > wr["run_started_at"]


def test_workflow_run_is_deterministic():
    """No Date.now/random: same inputs -> byte-identical output (reproducible)."""
    a = deployfeed.build_workflow_run(_run(), RELEASE_COMMIT, _inputs())
    b = deployfeed.build_workflow_run(_run(), RELEASE_COMMIT, _inputs())
    assert json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)


# --- the full webhook payload ----------------------------------------------


def test_webhook_payload_shape():
    p = deployfeed.build_webhook_payload(_run(), RELEASE_COMMIT, _inputs())
    assert p["action"] == "completed"
    assert p["workflow"]["name"] == deployfeed.DEPLOY_WORKFLOW_NAME
    assert p["workflow_run"]["head_sha"] == _run().release_sha
    assert p["repository"]["full_name"] == FORK_REPO["full_name"]
    assert p["sender"]["login"] == OWNER["login"]
    # personal fork -> no organization key (real upstream org payload has one)
    assert "organization" not in p


# --- the recorder envelope --------------------------------------------------


def test_envelope_matches_recorder_format():
    env = deployfeed.build_envelope(_run(), RELEASE_COMMIT, _inputs())
    assert env["source"] == "github"
    assert env["resource"] == "workflow_run"
    assert env["headers"]["x-github-event"] == "workflow_run"
    # raw_body round-trips (latin-1, exactly like the live recorder)
    body = json.loads(env["raw_body"].encode("latin-1").decode("latin-1"))
    assert body["workflow_run"]["head_sha"] == _run().release_sha
    assert env.get("reconstructed") is True


def test_envelope_signature_verifies_when_secret_given():
    env = deployfeed.build_envelope(_run(), RELEASE_COMMIT, _inputs(), secret="s3cret")
    sig = env["headers"]["x-hub-signature-256"]
    assert sig.startswith("sha256=")
    expected = hmac.new(
        b"s3cret", env["raw_body"].encode("latin-1"), hashlib.sha256
    ).hexdigest()
    assert sig == f"sha256={expected}"


def test_envelope_unsigned_by_default():
    """Reconstructed payloads are not delivered by GitHub, so absent a configured
    secret we do not fabricate a GitHub signature."""
    env = deployfeed.build_envelope(_run(), RELEASE_COMMIT, _inputs())
    assert "x-hub-signature-256" not in env["headers"]


# --- schema faithfulness against a REAL production run ----------------------


def test_workflow_run_keys_match_real_upstream_template():
    """Objective fidelity check: the generated workflow_run object carries exactly
    the field set of a real production 'AWS Deployment' run."""
    template = deployfeed.load_template()
    wr = deployfeed.build_workflow_run(_run(), RELEASE_COMMIT, _inputs())
    assert set(wr) == set(template), (
        f"schema drift: only-in-generated={set(wr) - set(template)}, "
        f"only-in-real={set(template) - set(wr)}"
    )


def test_deploy_fixture_name_is_deterministic_and_timestamped():
    name = deployfeed.deploy_fixture_name(_run())
    assert name.endswith(".json")
    assert name == deployfeed.deploy_fixture_name(_run())
    assert name.startswith("20260704T034625")
