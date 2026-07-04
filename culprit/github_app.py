"""The GitHub App write path — the ONE new write permission (offer-only).

``GitHubAppWriter`` is the *only* place Culprit writes to GitHub. It is a separate
module from the read client (``culprit/github_api.py``) — the read path is never
handed write scope. The App is granted exactly ``contents: write`` +
``pull_requests: write`` on the fork; **there is no merge call anywhere in this
module** (the permanent offer-only stance — Culprit drafts, humans merge).

Auth is the standard GitHub App flow: mint an RS256 App JWT (via ``pyjwt``, a
bounded generic primitive — no vendor SDK, the M3 httpx-not-SDK spirit), exchange
it for a short-lived installation token, then create a branch, write the
postmortem file, and open a PR — all via httpx REST.

Gated: without app id / private key / installation id the writer is inert
(``enabled`` is False; ``open_postmortem_pr`` returns None without any network),
so the pipeline falls back to dry-run.
"""

from __future__ import annotations

import base64
import time

import httpx

API_URL = "https://api.github.com"


class GitHubAppWriter:
    """Opens a postmortem PR on the fork. Never merges. Gated/inert without creds."""

    def __init__(
        self,
        app_id: str | None,
        private_key: str | None,
        installation_id: str | None,
        repo: str,
        *,
        base_url: str = API_URL,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.app_id = app_id
        self.private_key = private_key
        self.installation_id = installation_id
        self.repo = repo
        self.base_url = base_url
        self._client = client or httpx.AsyncClient(timeout=30.0)
        self._owns_client = client is None

    @property
    def enabled(self) -> bool:
        return bool(self.app_id and self.private_key and self.installation_id)

    def _app_jwt(self) -> str:
        """A short-lived RS256 App JWT (iss = app id), per GitHub's App auth."""
        import jwt

        now = int(time.time())
        payload = {"iat": now - 60, "exp": now + 540, "iss": self.app_id}
        return jwt.encode(payload, self.private_key, algorithm="RS256")

    async def _installation_token(self) -> str:
        """Exchange the App JWT for a short-lived installation access token."""
        resp = await self._client.post(
            f"{self.base_url}/app/installations/{self.installation_id}/access_tokens",
            headers={
                "Authorization": f"Bearer {self._app_jwt()}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        resp.raise_for_status()
        return resp.json()["token"]

    async def open_postmortem_pr(
        self,
        *,
        path: str,
        branch: str,
        title: str,
        body: str,
        base: str,
        pr_body: str,
        commit_message: str | None = None,
    ) -> dict | None:
        """Create the branch, write the file, and open the PR. Returns the PR dict.

        Never merges. Returns None (no network) when the writer is inert.
        """
        if not self.enabled:
            return None

        token = await self._installation_token()
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        repo = self.repo

        # 1. Base branch head SHA (the branch-off point).
        ref = await self._client.get(
            f"{self.base_url}/repos/{repo}/git/ref/heads/{base}", headers=headers
        )
        ref.raise_for_status()
        base_sha = ref.json()["object"]["sha"]

        # 2. Create the postmortem branch off that SHA.
        made = await self._client.post(
            f"{self.base_url}/repos/{repo}/git/refs",
            headers=headers,
            json={"ref": f"refs/heads/{branch}", "sha": base_sha},
        )
        made.raise_for_status()

        # 3. Write the postmortem file on the branch (contents API, base64).
        put = await self._client.put(
            f"{self.base_url}/repos/{repo}/contents/{path}",
            headers=headers,
            json={
                "message": commit_message or f"postmortem: {title}",
                "content": base64.b64encode(body.encode()).decode(),
                "branch": branch,
            },
        )
        put.raise_for_status()

        # 4. Open the PR into the base branch. NO merge call — humans merge.
        pull = await self._client.post(
            f"{self.base_url}/repos/{repo}/pulls",
            headers=headers,
            json={"title": title, "head": branch, "base": base, "body": pr_body},
        )
        pull.raise_for_status()
        data = pull.json()
        return {"html_url": data.get("html_url"), "number": data.get("number")}

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()
