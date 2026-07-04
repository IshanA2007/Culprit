"""Deploy-window reconstruction — the candidate set the ranker scores over.

Production has no ground-truth window; M2 derives it deterministically as
``compare(previous_head, release).commits`` (plan decision 5). In the eval, the
replay seeds a prior deploy at the run's ``base_sha`` so ``previous_head_sha`` is
``base_sha`` and the reconstruction matches the recorded window exactly.
"""

from __future__ import annotations

from culprit.github_api import GitHubClient
from culprit.models import Deploy


async def reconstruct_window(
    github: GitHubClient, base_sha: str | None, head_sha: str
) -> list[dict]:
    """Ordered candidate commits between ``base_sha`` and ``head_sha`` (inclusive of head)."""
    if not base_sha:
        return []
    data = await github.compare(base_sha, head_sha)
    return data.get("commits", [])


async def window_for_deploy(github: GitHubClient, deploy: Deploy) -> list[dict]:
    """The window for a deploy row, using its recorded ``previous_head_sha`` base."""
    return await reconstruct_window(github, deploy.previous_head_sha, deploy.head_sha)
