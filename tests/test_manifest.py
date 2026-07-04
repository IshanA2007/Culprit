"""Manifest-level corpus invariants (plan Task 9, the parts checkable without
recorded runs). The SHA-resolvability / anti-leakage-over-run-records checks
land in test_corpus.py once runs exist.
"""

from __future__ import annotations

from harness.config import FAULTS_DIR
from harness.manifest import ExpectedSignal, FaultClass, GroundTruth, load_manifest

FAULTS = load_manifest()


def test_manifest_nonempty():
    assert FAULTS, "manifest.yaml has no faults"


def test_every_code_fault_patch_exists_on_disk():
    for f in FAULTS:
        if f.fault_class is FaultClass.CODE:
            p = f.patch_path
            assert p and p.exists(), f"{f.id}: patch {f.patch} missing under {FAULTS_DIR}"


def test_ids_unique():
    ids = [f.id for f in FAULTS]
    assert len(ids) == len(set(ids))


def test_acceptance_counts():
    # plan Acceptance: >=12 scenarios, >=3 abstention, >=2 silent, exactly 1 baseline
    assert len(FAULTS) >= 12
    abstain = [f for f in FAULTS if f.ground_truth is GroundTruth.ABSTAIN]
    silent = [f for f in FAULTS if not f.sentry_visible]
    baseline = [f for f in FAULTS if f.fault_class is FaultClass.BASELINE]
    assert len(abstain) >= 3, f"need >=3 abstention faults, have {len(abstain)}"
    assert len(silent) >= 2, f"need >=2 silent faults, have {len(silent)}"
    assert len(baseline) == 1, f"need exactly 1 baseline, have {len(baseline)}"


def test_five_handoff_categories_present():
    # bad migration, template crash, code crash/endpoint, N+1/timeout, infra
    cats = {f.category for f in FAULTS}
    needles = ["migration", "template", "N+1", "infra"]
    for n in needles:
        assert any(n in c for c in cats), f"no fault category matching '{n}'"


def test_class_specific_rules():
    for f in FAULTS:
        if f.fault_class is FaultClass.CODE:
            assert f.patch and f.ground_truth is GroundTruth.CULPRIT_COMMIT
            assert f.expected_signal in (ExpectedSignal.EVENT_ALERT, ExpectedSignal.LOGS)
            assert f.trigger, f"{f.id}: code fault needs trigger requests"
        elif f.fault_class is FaultClass.INFRA:
            assert f.docker_action and f.ground_truth is GroundTruth.ABSTAIN
        elif f.fault_class is FaultClass.BASELINE:
            assert f.ground_truth is GroundTruth.NO_INCIDENT
            assert not f.patch and not f.docker_action


def test_silent_faults_use_logs_signal():
    # A silent fault (no Sentry event) must not claim a Sentry signal.
    for f in FAULTS:
        if not f.sentry_visible:
            assert f.expected_signal in (ExpectedSignal.LOGS, ExpectedSignal.NONE)
