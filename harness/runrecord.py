"""Run records — the eval cases.

Every scenario run writes one YAML record to ``runs/<ts>-<id>.yaml`` capturing
exactly what was deployed and what the ground truth is. These are the eval cases
Milestone 5 scores against: "top-3 of what?" is answered by ``window`` (the
recorded candidate commits), and the answer key is ``ground_truth`` +
``culprit_sha``.

This schema is deliberately self-describing and flat so a downstream consumer
can score without re-deriving anything — and so the anti-leakage tests (Task 9)
can assert the culprit is contained in the window but is never the release SHA.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path

import yaml

from harness.config import RUNS_DIR


@dataclass
class WindowCommit:
    sha: str
    message: str
    is_culprit: bool = False
    is_decoy: bool = False


@dataclass
class RunRecord:
    # Identity
    run_id: str  # "<ts>-<fault_id>"
    fault_id: str
    fault_class: str  # code | infra | baseline
    ground_truth: str  # culprit_commit | abstain | no_incident

    # Deploy window (the candidate set an eval consumer ranks over)
    base_sha: str  # the culprit-harness base the window was branched from
    release_sha: str  # window HEAD == Sentry release (decision 5: often a decoy)
    window: list[WindowCommit] = field(default_factory=list)

    # The answer key. None for abstain/no_incident.
    culprit_sha: str | None = None

    # Provenance / timing
    injected_at: str | None = None
    first_signal_at: str | None = None
    decoy_config: dict = field(default_factory=dict)

    # Where the recorded evidence landed (relative to repo root)
    fixture_paths: list[str] = field(default_factory=list)
    log_paths: list[str] = field(default_factory=list)

    # The deploy that shipped this window: a GitHub workflow_run ("AWS Deployment")
    # fixture whose head_sha == release_sha. This is the deploy-timeline half of
    # the M2 ingest contract (see harness/deployfeed.py). None until backfilled.
    deploy: str | None = None

    # The SNS/CloudWatch alarm delivery for a silent fault (M3): a synthesized
    # SNS Notification fixture (see harness/snsfeed.py). Only silent-fault + infra
    # dedup runs carry one; None otherwise. Backfilled by `backfill-sns`.
    sns: str | None = None

    # M4 postmortem inputs (see harness/discordfeed.py), backfilled by
    # `backfill-postmortem-inputs`:
    #  * fix_deploy — a rollback workflow_run shipping base_sha (the fixing commit);
    #    only code faults carry one (infra faults resolve via remediation, no code).
    #  * thread — the incident channel's Discord chat thread (the human narrative);
    #    every incident-producing run carries one; the baseline does not.
    fix_deploy: str | None = None
    thread: str | None = None

    def culprit_in_window(self) -> bool:
        return any(c.is_culprit and c.sha == self.culprit_sha for c in self.window)

    def culprit_is_release(self) -> bool:
        """The property that must NEVER be true for a code fault (label leakage)."""
        return self.culprit_sha is not None and self.culprit_sha == self.release_sha

    def to_dict(self) -> dict:
        return asdict(self)

    def write(self, runs_dir: Path | None = None) -> Path:
        runs_dir = runs_dir or RUNS_DIR
        runs_dir.mkdir(parents=True, exist_ok=True)
        path = runs_dir / f"{self.run_id}.yaml"
        path.write_text(yaml.safe_dump(self.to_dict(), sort_keys=False))
        return path


def load_run_record(path: Path) -> RunRecord:
    data = yaml.safe_load(path.read_text())
    window = [WindowCommit(**c) for c in data.pop("window", [])]
    return RunRecord(window=window, **data)


def load_all_run_records(runs_dir: Path | None = None) -> list[RunRecord]:
    runs_dir = runs_dir or RUNS_DIR
    if not runs_dir.exists():
        return []
    return [load_run_record(p) for p in sorted(runs_dir.glob("*.yaml"))]
