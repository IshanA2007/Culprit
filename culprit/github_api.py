"""Read-only GitHub client — evidence pinned to the deployed SHA (HANDOFF §4).

All reads target the fork at an immutable commit: ``compare`` for the window,
``commit`` for a candidate's diff, and GraphQL ``blame`` for which commit last
touched a stack-frame line (Sentry's suspect-commits mechanism). Responses are
cached to disk keyed by (repo, args); since every SHA is immutable the cache is
always valid, which keeps the 22-run eval inside the API rate limit.

Pure helpers (``window_commit_shas``, ``blame_commit_for_line``) are separate so
they can be unit-tested without a network.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import httpx

from culprit.config import REPO_ROOT

API_URL = "https://api.github.com"
GRAPHQL_URL = "https://api.github.com/graphql"

_BLAME_QUERY = """
query($owner:String!,$name:String!,$oid:GitObjectID!,$path:String!){
  repository(owner:$owner,name:$name){
    object(oid:$oid){
      ... on Commit {
        blame(path:$path){ ranges { startingLine endingLine commit { oid } } }
      }
    }
  }
}
"""


def window_commit_shas(compare_json: dict) -> list[str]:
    """Ordered candidate SHAs from a compare response (oldest -> release head)."""
    return [c["sha"] for c in compare_json.get("commits", [])]


def blame_commit_for_line(ranges: list[dict], lineno: int) -> str | None:
    """The commit oid whose blame range covers ``lineno`` (or None)."""
    for r in ranges:
        if r["startingLine"] <= lineno <= r["endingLine"]:
            return r["oid"]
    return None


class GitHubClient:
    """Minimal async GitHub REST+GraphQL client with an immutable-response cache."""

    def __init__(
        self,
        token: str | None,
        repo: str,
        *,
        base_url: str = API_URL,
        graphql_url: str = GRAPHQL_URL,
        cache_dir: str | Path | None = REPO_ROOT / ".cache" / "github",
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.repo = repo
        self.owner, _, self.name = repo.partition("/")
        self.base_url = base_url
        self.graphql_url = graphql_url

        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "culprit",
        }
        if token:
            headers["Authorization"] = f"Bearer {token}"
        self._client = client or httpx.AsyncClient(timeout=30.0, headers=headers)
        self._owns_client = client is None

        self.cache_dir = Path(cache_dir) if cache_dir else None
        if self.cache_dir:
            self.cache_dir.mkdir(parents=True, exist_ok=True)

    def _cached(self, key: str):
        if not self.cache_dir:
            return None
        path = self.cache_dir / f"{hashlib.sha256(key.encode()).hexdigest()[:24]}.json"
        return json.loads(path.read_text()) if path.exists() else None

    def _store(self, key: str, value) -> None:
        if not self.cache_dir:
            return
        path = self.cache_dir / f"{hashlib.sha256(key.encode()).hexdigest()[:24]}.json"
        path.write_text(json.dumps(value))

    async def _get_json(self, path: str) -> dict:
        resp = await self._client.get(f"{self.base_url}{path}")
        resp.raise_for_status()
        return resp.json()

    async def compare(self, base: str, head: str) -> dict:
        key = f"compare:{self.repo}:{base}...{head}"
        cached = self._cached(key)
        if cached is not None:
            return cached
        data = await self._get_json(f"/repos/{self.repo}/compare/{base}...{head}")
        self._store(key, data)
        return data

    async def commit(self, sha: str) -> dict:
        key = f"commit:{self.repo}:{sha}"
        cached = self._cached(key)
        if cached is not None:
            return cached
        data = await self._get_json(f"/repos/{self.repo}/commits/{sha}")
        self._store(key, data)
        return data

    async def blame(self, path: str, sha: str) -> list[dict]:
        """Blame ranges [{startingLine, endingLine, oid}] for ``path`` at ``sha``."""
        key = f"blame:{self.repo}:{sha}:{path}"
        cached = self._cached(key)
        if cached is not None:
            return cached
        resp = await self._client.post(
            self.graphql_url,
            json={
                "query": _BLAME_QUERY,
                "variables": {
                    "owner": self.owner,
                    "name": self.name,
                    "oid": sha,
                    "path": path,
                },
            },
        )
        resp.raise_for_status()
        payload = resp.json()
        obj = ((payload.get("data") or {}).get("repository") or {}).get("object") or {}
        ranges = [
            {
                "startingLine": r["startingLine"],
                "endingLine": r["endingLine"],
                "oid": r["commit"]["oid"],
            }
            for r in ((obj.get("blame") or {}).get("ranges") or [])
        ]
        self._store(key, ranges)
        return ranges

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()
