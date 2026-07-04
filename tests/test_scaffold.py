"""Scaffold smoke tests — the package imports, contracts load, CLI builds.

These are deliberately infra-free so CI is green from the first commit. The
corpus-invariant tests (anti-leakage, SHA resolvability, fixture shape) land in
Task 9 once recorded runs exist.
"""

from __future__ import annotations

import harness
from harness.cli import build_parser, main
from harness.manifest import load_manifest, validate
from harness.runrecord import RunRecord, WindowCommit


def test_package_version():
    assert harness.__version__


def test_manifest_loads_and_validates():
    faults = load_manifest()
    # Empty is fine at scaffold time; whatever is present must validate.
    validate(faults)
    assert isinstance(faults, list)


def test_cli_parser_builds():
    parser = build_parser()
    assert parser.prog == "culprit-harness"


def test_cli_faults_runs(capsys):
    assert main(["faults"]) == 0
    out = capsys.readouterr().out
    assert "faults" in out.lower() or "No faults" in out


def test_runrecord_leakage_helpers():
    rr = RunRecord(
        run_id="t",
        fault_id="f",
        fault_class="code",
        ground_truth="culprit_commit",
        base_sha="base",
        release_sha="deadbeef",
        window=[
            WindowCommit(sha="c0ffee", message="fix", is_culprit=True),
            WindowCommit(sha="deadbeef", message="decoy", is_decoy=True),
        ],
        culprit_sha="c0ffee",
    )
    # Culprit is in the window but is NOT the release SHA — the property the
    # anti-leakage invariants enforce.
    assert rr.culprit_in_window()
    assert not rr.culprit_is_release()
