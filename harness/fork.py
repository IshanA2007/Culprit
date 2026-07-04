"""Fork / working-clone management: build the deploy window.

Owns the harness's checkout of theCourseForum2 (``config.TCF_WORK_DIR``) and the
git mechanics of materializing a deploy window:

1. reset the working clone to the ``culprit-harness`` base;
2. create ``fault/<id>-<ts>`` off it;
3. commit decoys + the fault commit with the culprit at a NON-HEAD position for
   multi-commit windows (so the culprit is never the window head / release SHA —
   the anti-leakage invariant; single-commit windows are the trivial case);
4. tag every window commit and push branch + tags to the fork (retained forever
   — M2 resolves these SHAs via the GitHub API, so they must stay fetchable);
5. return the ordered (sha, message, is_culprit) list for the run record.
"""

from __future__ import annotations

import random
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
    return subprocess.run(
        ["git", *args], cwd=cwd, text=True, capture_output=True, check=True
    ).stdout.strip()


def current_base_sha(cwd: Path = TCF_WORK_DIR) -> str:
    """SHA of the harness branch the window is built on."""
    return _git("rev-parse", HARNESS_BRANCH, cwd=cwd)


def reset_to_base(cwd: Path = TCF_WORK_DIR) -> None:
    """Return the working clone to a clean ``culprit-harness`` checkout."""
    _git("checkout", "--force", HARNESS_BRANCH, cwd=cwd)
    _git("reset", "--hard", HARNESS_BRANCH, cwd=cwd)
    _git("clean", "-fd", cwd=cwd)


def _apply_and_commit(patch_path: Path, message: str, cwd: Path) -> str:
    """Apply one patch (3-way for robustness when stacked) and commit it."""
    subprocess.run(
        ["git", "apply", "--3way", str(patch_path)],
        cwd=cwd,
        text=True,
        capture_output=True,
        check=True,
    )
    _git("add", "-A", cwd=cwd)
    _git("commit", "--no-verify", "-m", message, cwd=cwd)
    return _git("rev-parse", "HEAD", cwd=cwd)


def _culprit_index(size: int, position: str, rng: random.Random) -> int:
    """Pick the culprit's slot. For size>1 it is NEVER the head (index size-1)."""
    if size == 1:
        return 0
    last_non_head = size - 2  # 0..size-2 are non-head slots
    if position in ("head", "newest"):
        # head is disallowed for multi-commit windows (would make release==culprit);
        # degrade to the latest non-head slot.
        return last_non_head
    if position in ("first", "oldest", "early"):
        return 0
    if position == "middle":
        return min((size - 1) // 2, last_non_head)
    return rng.randint(0, last_non_head)  # random / tail / unknown


def build_window(
    fault: Fault,
    decoys: list[Decoy],
    *,
    size: int,
    culprit_position: str = "random",
    timestamp: str,
    seed: int | None = None,
    cwd: Path = TCF_WORK_DIR,
) -> BuiltWindow:
    """Materialize decoys + the fault commit into an ordered window branch.

    ``timestamp`` is passed in (never generated here) so runs are reproducible
    and the branch name is stable across a resumed run. For code faults, exactly
    one slot is the fault; the rest are decoys. Baseline/infra callers pass a
    fault whose patch is None and rely on decoys only (size == len(decoys)).
    """
    rng = random.Random(seed)
    is_code_fault = fault.patch_path is not None

    if is_code_fault:
        n_decoys = size - 1
        if n_decoys < 0:
            raise ValueError("size must be >= 1")
        if len(decoys) < n_decoys:
            raise ValueError(f"need {n_decoys} decoys, have {len(decoys)}")
        chosen = list(decoys[:n_decoys])
        culprit_idx = _culprit_index(size, culprit_position, rng)
    else:
        # decoy-only window (baseline / infra runs on a benign release)
        if len(decoys) < size:
            raise ValueError(f"decoy-only window of {size} needs {size} decoys")
        chosen = list(decoys[:size])
        culprit_idx = -1

    reset_to_base(cwd)
    base_sha = current_base_sha(cwd)
    branch = f"fault/{fault.id}-{timestamp}"
    _git("checkout", "-B", branch, HARNESS_BRANCH, cwd=cwd)

    commits: list[WindowCommit] = []
    decoy_iter = iter(chosen)
    for i in range(size):
        if i == culprit_idx:
            sha = _apply_and_commit(fault.patch_path, fault.commit_message, cwd)
            commits.append(
                WindowCommit(sha=sha, message=fault.commit_message, is_culprit=True)
            )
        else:
            d = next(decoy_iter)
            sha = _apply_and_commit(d.patch_path, d.message, cwd)
            commits.append(WindowCommit(sha=sha, message=d.message, is_decoy=True))

    return BuiltWindow(
        base_sha=base_sha,
        release_sha=commits[-1].sha,
        commits=commits,
        branch=branch,
    )


def tag_and_push(
    window: BuiltWindow, *, remote: str = "origin", cwd: Path = TCF_WORK_DIR
) -> list[str]:
    """Tag every window commit and push branch + tags to the fork. Retained refs.

    Returns the created tag names. Tags guarantee the SHAs stay fetchable for M2
    even if the branch is later deleted.
    """
    tags = []
    for i, c in enumerate(window.commits):
        tag = f"culprit/{window.branch.replace('fault/', '')}/{i:02d}-{c.sha[:8]}"
        _git("tag", "-f", tag, c.sha, cwd=cwd)
        tags.append(tag)
    _git("push", "--force", remote, window.branch, cwd=cwd)
    for tag in tags:
        _git("push", "--force", remote, tag, cwd=cwd)
    return tags
