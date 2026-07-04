"""Task 4 — the Discord thread reader (the human-narrative half of the postmortem).

Mirrors ``culprit/cloudwatch.py``'s provider shape: a ``ThreadReader`` protocol
with an offline ``FixtureThreadReader`` (reads ``fixtures/discord/*.json``) and a
gated ``DiscordThreadReader`` (httpx ``GET /channels/{id}/messages`` with a bot
token). Absent token -> inert (read returns []), so the postmortem omits the
Discussion section cleanly and is still complete without it.
"""

from __future__ import annotations

import json

import httpx

from culprit.discord_read import DiscordThreadReader, FixtureThreadReader


def _write(tmp_path, name, payload) -> str:
    p = tmp_path / name
    p.write_text(json.dumps(payload))
    return str(p)


# --- FixtureThreadReader -----------------------------------------------------


async def test_fixture_reader_normalizes_raw_discord_messages(tmp_path):
    # newest-first raw Discord shape (author is an object) -> chronological normal
    path = _write(
        tmp_path,
        "thread.json",
        {
            "messages": [
                {"author": {"username": "bob"}, "content": "confirmed green"},
                {"author": {"username": "alice"}, "content": "rolling back now"},
            ]
        },
    )
    reader = FixtureThreadReader(path)
    assert reader.enabled
    msgs = await reader.read()
    assert [m["author"] for m in msgs] == ["bob", "alice"]
    assert msgs[1]["content"] == "rolling back now"


async def test_fixture_reader_inert_without_a_path():
    reader = FixtureThreadReader(None)
    assert reader.enabled is False
    assert await reader.read() == []


# --- DiscordThreadReader (gated; httpx mocked) -------------------------------


async def test_discord_reader_reads_and_reverses_to_chronological():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bot tok"
        assert "/channels/123/messages" in str(request.url)
        # Discord returns newest-first
        return httpx.Response(
            200,
            json=[
                {"author": {"username": "bob"}, "content": "green", "timestamp": "t2"},
                {
                    "author": {"username": "alice"},
                    "content": "rollback",
                    "timestamp": "t1",
                },
            ],
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    reader = DiscordThreadReader("tok", "123", client=client)
    try:
        assert reader.enabled
        msgs = await reader.read()
    finally:
        await client.aclose()
    assert [m["author"] for m in msgs] == ["alice", "bob"]  # reversed to chronological


async def test_discord_reader_inert_without_token():
    reader = DiscordThreadReader(None, "123")
    assert reader.enabled is False
    assert await reader.read() == []
