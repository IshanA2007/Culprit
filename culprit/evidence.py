"""Evidence gathering — diffs + per-frame blame, pinned to the release SHA.

For each window candidate we record its diff (files changed). For each Sentry
stack frame we blame the file at ``release_sha`` to find the commit that last
touched that line; a blame landing on a window candidate is the signal the ranker
scores. All reads are pinned to the deployed SHA (HANDOFF §4).
"""

from __future__ import annotations

from culprit.github_api import blame_commit_for_line
from culprit.models import Evidence


def _commit_message(commit: dict) -> str | None:
    # REST commit response nests the message under "commit"; fakes may inline it.
    nested = (commit.get("commit") or {}).get("message")
    return nested or commit.get("message")


async def gather_evidence(
    session,
    github,
    *,
    incident_id: int,
    window_shas: list[str],
    frames: list[dict],
    release_sha: str,
) -> list[Evidence]:
    """Persist diff + blame Evidence for an incident; return the created rows."""
    window_set = set(window_shas)
    evidence: list[Evidence] = []

    for sha in window_shas:
        commit = await github.commit(sha)
        files = [f.get("filename") for f in commit.get("files", [])]
        patch = "\n".join(f.get("patch", "") for f in commit.get("files", []))
        row = Evidence(
            incident_id=incident_id,
            commit_sha=sha,
            kind="diff",
            payload={
                "files": files,
                "patch": patch,
                "message": _commit_message(commit),
            },
        )
        session.add(row)
        evidence.append(row)

    for frame in frames:
        path = frame.get("file")
        lineno = frame.get("lineno")
        if not path or lineno is None:
            continue
        ranges = await github.blame(path, release_sha)
        oid = blame_commit_for_line(ranges, lineno)
        row = Evidence(
            incident_id=incident_id,
            commit_sha=oid,
            kind="blame",
            payload={
                "file": path,
                "lineno": lineno,
                "function": frame.get("function"),
            },
            cited=oid in window_set,
        )
        session.add(row)
        evidence.append(row)

    await session.commit()
    return evidence
