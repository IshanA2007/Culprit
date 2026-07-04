"""Runbook corpus invariants (plan Task 1).

The corpus (``runbooks/*.md``) is the offer-only remediation catalog authored
for tCF's real failure modes (their ``iac/``: RDS, ElastiCache, ECS Fargate,
ALB, Cognito, CloudFront). ``culprit/runbooks.py`` loads + validates it. These
tests pin the contract (unique ids, required frontmatter, offer-only stance) and
prove the Task 9 label map is authorable: every incident-producing fault in the
manifest has exactly one intended runbook that exists in the corpus.
"""

from __future__ import annotations

import pytest

from culprit.runbooks import RunbookError, load_runbooks, validate
from harness.manifest import GroundTruth, load_manifest

RUNBOOKS = load_runbooks()
FAULTS = load_manifest()

REQUIRED_FIELDS = (
    "id",
    "title",
    "summary",
    "failure_mode",
    "symptoms",
    "checks",
    "steps",
    "rollback",
)

# The intended fault -> runbook mapping (Task 1 completeness proof; the Task 9
# scorer-only label map is a superset-authorable-from this). Every
# incident-producing fault (ground_truth != no_incident) maps to exactly one.
EXPECTED_RUNBOOK = {
    "template-noreversematch-instructor-card": "app-error-spike-after-deploy",
    "search-fielderror-500": "app-error-spike-after-deploy",
    "vote-duplicate-integrityerror": "app-error-spike-after-deploy",
    "landing-import-crash-bad-deploy": "rollback-bad-deploy",
    "bad-migration-drop-semester-season": "bad-migration-rollback",
    "n-plus-one-section-instructor-prefetch": "perf-latency-regression",
    "cartesian-join-gpa-annotation-timeout": "perf-latency-regression",
    "search-silent-zero-results": "search-zero-results",
    "bad-migration-drop-trigram-gin-indexes": "perf-latency-regression",
    "redis-down": "redis-elasticache-down",
    "db-stopped": "rds-outage-conn-exhaustion",
    "gunicorn-worker-oom": "ecs-oom-crashloop",
}


def test_corpus_loads_between_8_and_12():
    # HANDOFF §5 / plan decision 11: 8–12 runbooks fit in a prompt.
    assert 8 <= len(RUNBOOKS) <= 12, f"corpus has {len(RUNBOOKS)} runbooks"


def test_ids_unique():
    ids = [r.id for r in RUNBOOKS]
    assert len(ids) == len(set(ids)), "duplicate runbook ids"


def test_every_runbook_has_required_fields():
    for r in RUNBOOKS:
        for f in REQUIRED_FIELDS:
            val = getattr(r, f)
            assert val, f"{r.id}: empty required field {f!r}"


def test_list_fields_are_nonempty_lists():
    for r in RUNBOOKS:
        for f in ("symptoms", "checks", "steps"):
            val = getattr(r, f)
            assert isinstance(val, list) and val, (
                f"{r.id}: {f} must be a non-empty list"
            )


def test_offer_only_no_auto_execution_declared():
    # Permanent stance (HANDOFF §3): Culprit offers, never executes. No runbook
    # may declare an auto-execution affordance in its contract.
    banned = {"auto_execute", "autorun", "execute", "run_command", "auto_run"}
    for r in RUNBOOKS:
        keys = set(r.frontmatter)
        assert not (keys & banned), (
            f"{r.id}: declares auto-execution key(s) {keys & banned}"
        )


def test_every_incident_producing_fault_maps_to_exactly_one_existing_runbook():
    corpus_ids = {r.id for r in RUNBOOKS}
    incident_faults = [
        f for f in FAULTS if f.ground_truth is not GroundTruth.NO_INCIDENT
    ]
    for f in incident_faults:
        assert f.id in EXPECTED_RUNBOOK, f"{f.id}: no intended runbook"
        rb = EXPECTED_RUNBOOK[f.id]
        assert rb in corpus_ids, f"{f.id}: intended runbook {rb!r} not in corpus"


def test_no_orphan_runbook_ids_in_mapping():
    corpus_ids = {r.id for r in RUNBOOKS}
    for fault_id, rb in EXPECTED_RUNBOOK.items():
        assert rb in corpus_ids, f"mapping for {fault_id} points at missing {rb!r}"


def test_by_id_lookup():
    corpus = {r.id: r for r in RUNBOOKS}
    assert "rollback-bad-deploy" in corpus
    assert corpus["rollback-bad-deploy"].failure_mode


def test_validate_rejects_duplicate_ids():
    dupe = list(RUNBOOKS) + [RUNBOOKS[0]]
    with pytest.raises(RunbookError):
        validate(dupe)
