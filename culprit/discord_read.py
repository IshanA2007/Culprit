"""The Discord thread reader — the human-narrative half of the postmortem.

The incident brief is posted in a Discord channel; the ensuing chat thread is the
human story that complements the machine timeline (HANDOFF §4). Two impls behind
one ``ThreadReader`` protocol (mirroring ``culprit/cloudwatch.py``'s LogsProvider):

* ``FixtureThreadReader`` — reads one committed ``fixtures/discord/*.json`` thread
  (offline; the eval + demo path);
* ``DiscordThreadReader`` — ``GET /channels/{id}/messages`` via httpx with a
  **read-scoped** bot token (the read-only stance applied to chat), **gated** on
  the token + channel id. Absent -> inert (``read`` returns ``[]``), so the
  postmortem's Discussion section is omitted cleanly.

Both normalize to ``[{author, content, timestamp}]`` in chronological order — the
shape ``culprit.postmortem`` renders. No write scope is ever requested here.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Protocol

import httpx

API_URL = "https://discord.com/api/v10"


def _normalize(message: dict) -> dict:
    """One raw message -> ``{author, content, timestamp}`` (author may be nested)."""
    author = message.get("author")
    if isinstance(author, dict):
        author = author.get("username") or author.get("global_name")
    return {
        "author": author,
        "content": message.get("content"),
        "timestamp": message.get("timestamp"),
    }


class ThreadReader(Protocol):
    """Serves the incident channel's messages (rendered by culprit.postmortem)."""

    @property
    def enabled(self) -> bool: ...

    async def read(self) -> list[dict]: ...


class FixtureThreadReader:
    """Reads one committed ``fixtures/discord/*.json`` thread (offline)."""

    def __init__(self, thread_path: Path | str | None):
        self.thread_path = Path(thread_path) if thread_path else None

    @property
    def enabled(self) -> bool:
        return self.thread_path is not None and self.thread_path.exists()

    async def read(self) -> list[dict]:
        if not self.enabled:
            return []
        data = json.loads(self.thread_path.read_text())
        messages = data.get("messages") if isinstance(data, dict) else data
        return [_normalize(m) for m in (messages or [])]


class DiscordThreadReader:
    """Live channel read via a read-scoped bot token (gated; inert without)."""

    def __init__(
        self,
        bot_token: str | None,
        channel_id: str | None,
        *,
        client: httpx.AsyncClient | None = None,
        base_url: str = API_URL,
    ):
        self.bot_token = bot_token
        self.channel_id = channel_id
        self.base_url = base_url
        self._client = client or httpx.AsyncClient(timeout=15.0)
        self._owns_client = client is None

    @property
    def enabled(self) -> bool:
        return bool(self.bot_token and self.channel_id)

    async def read(self) -> list[dict]:
        if not self.enabled:
            return []
        resp = await self._client.get(
            f"{self.base_url}/channels/{self.channel_id}/messages",
            headers={"Authorization": f"Bot {self.bot_token}"},
            params={"limit": 100},
        )
        resp.raise_for_status()
        # Discord returns newest-first; reverse to chronological for the timeline.
        return [_normalize(m) for m in reversed(resp.json())]

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()
