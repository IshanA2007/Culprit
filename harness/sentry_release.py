"""sentry-cli wrapper — associate the deploy window with a Sentry release.

Per window (plan decision 5): the release name is the **window HEAD SHA** (which
is frequently a decoy, so ``release`` never functions as the answer key), and we
associate the window's commits so each Sentry issue self-annotates its candidate
range:

    sentry-cli releases new <HEAD>
    sentry-cli releases set-commits <HEAD> --local
    sentry-cli releases finalize <HEAD>

Invariant asserted here: at least one commit with a non-empty patch_set is
associated (``--local`` requires a full, non-shallow clone). If not, the run
fails loudly rather than recording a release with no commit range.

STATUS: skeleton — the eval-critical invariant (culprit ∈ release range, never
release == culprit) is enforced by the runner + Task 9 tests; this module shells
out to sentry-cli, which needs the Sentry account (an ask-boundary).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from harness.config import TCF_WORK_DIR


def associate_release(head_sha: str, *, cwd: Path = TCF_WORK_DIR) -> int:
    """Create + set-commits --local + finalize. Returns # commits associated.

    Raises if zero commits with a patch_set were associated (a shallow clone or
    a misconfigured org would silently produce an empty range otherwise).
    """
    raise NotImplementedError(
        "associate_release shells out to sentry-cli — needs the Sentry account "
        "and SENTRY_AUTH_TOKEN (plan Task 4, ask-boundary)"
    )


def _sentry_cli(*args: str, cwd: Path = TCF_WORK_DIR) -> str:
    return subprocess.check_output(["sentry-cli", *args], cwd=cwd, text=True).strip()
