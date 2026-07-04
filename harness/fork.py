"""Fork / working-clone management: build the deploy window.

Owns the harness's checkout of theCourseForum2 (``config.TCF_WORK_DIR``) and the
git mechanics of materializing a deploy window:

1. reset the working clone to the ``culprit-harness`` base;
2. create ``fault/<id>-<ts>`` off it;
3. commit decoys + the fault commit with the culprit at a randomized position
   (so the culprit is NOT reliably the window head — anti-leakage);
4. tag every commit and push the refs to the fork (**retained forever** — M2
   resolves these SHAs via the GitHub API, so they must stay fetchable);
5. capture the ordered (sha, message, is_culprit) list for the run record.

STATUS: skeleton — interfaces are settled; git operations are implemented in
Task 7 once the working clone exists. Signatures below are the contract the
runner and tests build against.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

from harness.config import HARNESS_BRANCH, TCF_WORK_DIR
from harness.decoys import Decoy
from harness.manifest import Fault
from harness.runrecord import WindowCommit


@dataclass
class BuiltWindow:
    base_sha: str
    release_sha: str  # window HEAD
    commits: list[WindowCommit]  # ordered oldest -> newest
    branch: str


def _git(*args: str, cwd: Path = TCF_WORK_DIR) -> str:
    return subprocess.check_output(["git", *args], cwd=cwd, text=True).strip()


def current_base_sha(cwd: Path = TCF_WORK_DIR) -> str:
    """SHA of the harness branch the window is built on."""
    return _git("rev-parse", HARNESS_BRANCH, cwd=cwd)


def build_window(
    fault: Fault,
    decoys: list[Decoy],
    *,
    culprit_position: str = "random",
    timestamp: str,
    cwd: Path = TCF_WORK_DIR,
) -> BuiltWindow:
    """Materialize decoys + the fault commit into an ordered window branch.

    ``timestamp`` is passed in (never generated here) so runs are reproducible
    and the branch name is stable across a resumed run.
    """
    raise NotImplementedError(
        "build_window is implemented in Task 7 against the working clone"
    )


def tag_and_push(window: BuiltWindow, *, remote: str = "origin") -> None:
    """Tag every window commit and push refs to the fork. Refs are retained."""
    raise NotImplementedError("tag_and_push is implemented in Task 7 (needs the fork)")


def reset_to_base(cwd: Path = TCF_WORK_DIR) -> None:
    """Return the working clone to a clean ``culprit-harness`` checkout."""
    raise NotImplementedError("reset_to_base is implemented in Task 7")
