"""sentry-cli wrapper — associate the deploy window with a Sentry release.

Per window (plan decision 5): the release name is the **window HEAD SHA** (which
is frequently a decoy, so ``release`` never functions as the answer key), and we
associate the window's commits so each Sentry issue self-annotates its candidate
range:

    sentry-cli releases new <HEAD>
    sentry-cli releases set-commits <HEAD> --local
    sentry-cli releases finalize <HEAD>

Invariant asserted here: at least one commit is associated with the release
(``--local`` requires a full, non-shallow clone — the working clone is full). If
zero commits associate, the run fails loudly rather than recording a release
with no candidate range.

Runtime env: SENTRY_AUTH_TOKEN, SENTRY_ORG, SENTRY_PROJECT (sentry-cli reads
these); SENTRY_URL defaults to https://sentry.io.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import httpx

from harness.config import TCF_WORK_DIR


def _sentry_cli(*args: str, cwd: Path = TCF_WORK_DIR) -> str:
    return subprocess.run(
        ["sentry-cli", *args], cwd=cwd, text=True, capture_output=True, check=True
    ).stdout.strip()


def associate_release(head_sha: str, *, cwd: Path = TCF_WORK_DIR) -> int:
    """new + set-commits --local + finalize. Returns # commits associated.

    Raises if zero commits associate (a shallow clone or a misconfigured org
    would otherwise silently produce a release with an empty candidate range).
    """
    _sentry_cli("releases", "new", head_sha, cwd=cwd)
    _sentry_cli("releases", "set-commits", head_sha, "--local", cwd=cwd)
    _sentry_cli("releases", "finalize", head_sha, cwd=cwd)

    n = count_associated_commits(head_sha)
    if n < 1:
        raise RuntimeError(
            f"release {head_sha[:12]} associated 0 commits — set-commits --local "
            "found no range (shallow clone? wrong org/project?)"
        )
    return n


def count_associated_commits(version: str) -> int:
    """Count commits Sentry has associated with the release, via its API."""
    org = os.environ["SENTRY_ORG"]
    token = os.environ["SENTRY_AUTH_TOKEN"]
    base = os.environ.get("SENTRY_URL", "https://sentry.io").rstrip("/")
    url = f"{base}/api/0/organizations/{org}/releases/{version}/commits/"
    resp = httpx.get(url, headers={"Authorization": f"Bearer {token}"}, timeout=15)
    resp.raise_for_status()
    return len(resp.json())


def _api() -> tuple[str, str, str, dict[str, str]]:
    org = os.environ["SENTRY_ORG"]
    project = os.environ["SENTRY_PROJECT"]
    base = os.environ.get("SENTRY_URL", "https://sentry.io").rstrip("/")
    headers = {"Authorization": f"Bearer {os.environ['SENTRY_AUTH_TOKEN']}"}
    return org, project, base, headers


def purge_environment_issues(environment: str = "fault-harness") -> int:
    """DELETE every issue in the fault-harness environment.

    Called between sequential runs so each fault run creates a genuinely NEW
    Sentry issue — the "A new issue is created" alert then fires every time, and
    the per-issue ~5-min action interval never suppresses a run's webhook
    (plan decision 4). Runs are sequential (shared web/db), so scoping by
    environment is precise.
    """
    org, project, base, headers = _api()
    listed = httpx.get(
        f"{base}/api/0/projects/{org}/{project}/issues/",
        headers=headers,
        params={
            "query": f"is:unresolved environment:{environment}",
            "statsPeriod": "24h",
        },
        timeout=15,
    )
    listed.raise_for_status()
    ids = [i["id"] for i in listed.json()]
    for issue_id in ids:
        httpx.delete(
            f"{base}/api/0/organizations/{org}/issues/{issue_id}/",
            headers=headers,
            timeout=15,
        )
    return len(ids)
