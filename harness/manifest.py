"""Fault manifest — the central contract.

``harness/faults/manifest.yaml`` describes every fault: what it is, how to
inject it, how to trigger it, what signal it should produce, and — critically —
its ground-truth label for the eval. Fault patches, the scenario runner, and the
corpus invariant tests all read this schema, so it is defined once here.

Ground-truth vocabulary (the eval's answer key):

* ``culprit_commit`` — a code fault. The answer is a specific commit *contained
  in* the recorded deploy window (never the release/window-head SHA — see the
  plan's decision 5 anti-leakage rule).
* ``abstain`` — an infrastructural fault (Redis down, DB down, OOM). The correct
  answer is to decline to blame any commit. Runs on top of a fresh *benign*
  release so abstention means declining to blame innocent commits, not noticing
  a missing field.
* ``no_incident`` — the negative control (benign deploy). No fault, no culprit;
  lets the eval report a false-positive rate.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from harness.config import FAULTS_DIR, MANIFEST_PATH


class FaultClass(enum.StrEnum):
    CODE = "code"
    INFRA = "infra"
    BASELINE = "baseline"


class GroundTruth(enum.StrEnum):
    CULPRIT_COMMIT = "culprit_commit"
    ABSTAIN = "abstain"
    NO_INCIDENT = "no_incident"


class ExpectedSignal(enum.StrEnum):
    EVENT_ALERT = "event_alert"  # Sentry issue-alert action webhook (has frames)
    ISSUE = "issue"  # Sentry issue webhook (has counts)
    LOGS = "logs"  # silent to Sentry; only gunicorn/docker stderr capture
    NONE = "none"  # negative control — no signal at all


@dataclass(frozen=True)
class TriggerRequest:
    path: str
    method: str = "GET"


@dataclass(frozen=True)
class WindowSpec:
    """How the deploy window is built for this fault.

    ``sizes`` — the total commit counts to record this fault at (e.g. [1, 4]
    means one single-commit window and one 4-commit window). Each (fault ×
    size) pair is a distinct eval case (plan Task 8).

    ``culprit_positions`` — where the culprit sits in multi-commit windows:
    ``head`` (window head — allowed, but must be the minority), ``middle``,
    ``tail``, or ``random``. Enforces the anti-leakage property that the culprit
    is not reliably the newest commit.
    """

    sizes: tuple[int, ...] = (1,)
    culprit_positions: tuple[str, ...] = ("random",)


@dataclass(frozen=True)
class Fault:
    id: str
    category: str  # handoff failure-category label (e.g. "template crash")
    fault_class: FaultClass
    commit_message: str
    ground_truth: GroundTruth
    expected_signal: ExpectedSignal
    expected_symptom: str
    sentry_visible: bool
    requires_auth: bool = False
    passes_tcf_tests: bool = True
    # Code faults carry a patch; infra/baseline carry a docker_action or nothing.
    patch: str | None = None
    docker_action: str | None = None
    trigger: tuple[TriggerRequest, ...] = ()
    throttle_seconds: float = 2.0
    window: WindowSpec = field(default_factory=WindowSpec)
    notes: str = ""

    @property
    def patch_path(self) -> Path | None:
        return FAULTS_DIR / self.patch if self.patch else None


def _parse_fault(raw: dict[str, Any]) -> Fault:
    trigger = tuple(
        TriggerRequest(path=t["path"], method=t.get("method", "GET"))
        for t in raw.get("trigger", {}).get("requests", [])
    )
    win = raw.get("window", {})
    window = WindowSpec(
        sizes=tuple(win.get("sizes", [1])),
        culprit_positions=tuple(win.get("culprit_positions", ["random"])),
    )
    return Fault(
        id=raw["id"],
        category=raw["category"],
        fault_class=FaultClass(raw["fault_class"]),
        commit_message=raw["commit_message"],
        ground_truth=GroundTruth(raw["ground_truth"]),
        expected_signal=ExpectedSignal(raw["expected_signal"]),
        expected_symptom=raw["expected_symptom"],
        sentry_visible=bool(raw["sentry_visible"]),
        requires_auth=bool(raw.get("requires_auth", False)),
        passes_tcf_tests=bool(raw.get("passes_tcf_tests", True)),
        patch=raw.get("patch"),
        docker_action=raw.get("docker_action"),
        trigger=trigger,
        throttle_seconds=float(raw.get("trigger", {}).get("throttle_seconds", 2.0)),
        window=window,
        notes=raw.get("notes", ""),
    )


def load_manifest(path: Path | None = None) -> list[Fault]:
    """Parse and validate ``manifest.yaml`` into a list of Faults."""
    path = path or MANIFEST_PATH
    if not path.exists():
        return []
    data = yaml.safe_load(path.read_text()) or {}
    faults = [_parse_fault(f) for f in data.get("faults", [])]
    validate(faults)
    return faults


class ManifestError(ValueError):
    """Raised when the manifest violates a structural invariant."""


def validate(faults: list[Fault]) -> None:
    """Structural invariants that must hold regardless of the recorded corpus.

    Corpus-level invariants (SHAs resolvable, culprit contained in window, etc.)
    live in the tests/ suite because they need recorded run records; these are
    the ones checkable from the manifest alone.
    """
    ids = [f.id for f in faults]
    dupes = {i for i in ids if ids.count(i) > 1}
    if dupes:
        raise ManifestError(f"duplicate fault ids: {sorted(dupes)}")

    for f in faults:
        if f.fault_class is FaultClass.CODE:
            if not f.patch:
                raise ManifestError(f"{f.id}: code fault must define a patch")
            if f.ground_truth is not GroundTruth.CULPRIT_COMMIT:
                raise ManifestError(
                    f"{f.id}: code fault must have ground_truth=culprit_commit"
                )
        elif f.fault_class is FaultClass.INFRA:
            if not f.docker_action:
                raise ManifestError(f"{f.id}: infra fault must define a docker_action")
            if f.ground_truth is not GroundTruth.ABSTAIN:
                raise ManifestError(
                    f"{f.id}: infra fault must have ground_truth=abstain"
                )
        elif f.fault_class is FaultClass.BASELINE:
            if f.ground_truth is not GroundTruth.NO_INCIDENT:
                raise ManifestError(
                    f"{f.id}: baseline must have ground_truth=no_incident"
                )
            if f.patch or f.docker_action:
                raise ManifestError(f"{f.id}: baseline must not inject anything")

        # A Sentry-visible fault must name a Sentry signal; a silent one must not.
        if f.sentry_visible and f.expected_signal in (
            ExpectedSignal.LOGS,
            ExpectedSignal.NONE,
        ):
            raise ManifestError(
                f"{f.id}: sentry_visible but expected_signal={f.expected_signal.value}"
            )
