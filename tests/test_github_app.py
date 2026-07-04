"""Task 6 — the GitHub App write path (the ONE new write permission).

GitHubAppWriter mints an App JWT (RS256), exchanges it for an installation token,
then creates a branch + writes the postmortem file + opens a PR — and **never
merges** (offer-only, permanent). All exercised against a mocked httpx transport
with a real RSA key, so the JWT is genuinely signed but no network is touched. An
inert writer (no creds) opens nothing (dry-run falls back to it).
"""

from __future__ import annotations

import base64

import httpx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from culprit.github_app import GitHubAppWriter

_REPO = "IshanA2007/theCourseForum2"


def _rsa_pem() -> str:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()


def _pr_kwargs() -> dict:
    return {
        "path": "postmortems/2026-07-04-fielderror.md",
        "branch": "culprit/postmortem-7-fielderror",
        "title": "Postmortem: FieldError",
        "body": "# Postmortem\n\nbody text\n",
        "base": "master",
        "pr_body": "Culprit drafted this. Humans merge.",
    }


def test_writer_inert_without_credentials():
    writer = GitHubAppWriter(None, None, None, _REPO)
    assert writer.enabled is False


async def test_writer_inert_writer_opens_nothing():
    calls: list = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(200, json={})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    writer = GitHubAppWriter(None, None, None, _REPO, client=client)
    try:
        result = await writer.open_postmortem_pr(**_pr_kwargs())
    finally:
        await client.aclose()
    assert result is None
    assert calls == []  # no network at all when inert


async def test_writer_opens_pr_and_never_merges():
    calls: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        calls.append((request.method, path))
        if path.endswith("/access_tokens"):
            return httpx.Response(201, json={"token": "ghs_installation_token"})
        if "/git/ref/heads/" in path:
            return httpx.Response(200, json={"object": {"sha": "base_sha_123"}})
        if path.endswith("/git/refs"):
            return httpx.Response(201, json={"ref": "refs/heads/culprit/..."})
        if "/contents/" in path:
            return httpx.Response(201, json={"content": {"path": "postmortems/x.md"}})
        if path.endswith("/pulls"):
            return httpx.Response(
                201,
                json={
                    "html_url": "https://github.com/IshanA2007/theCourseForum2/pull/5",
                    "number": 5,
                },
            )
        if "/merge" in path:  # the offer-only guarantee
            raise AssertionError("Culprit must NEVER merge a postmortem PR")
        return httpx.Response(404, json={"message": f"unexpected {path}"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    writer = GitHubAppWriter("app-id", _rsa_pem(), "inst-1", _REPO, client=client)
    try:
        result = await writer.open_postmortem_pr(**_pr_kwargs())
    finally:
        await client.aclose()

    assert result == {
        "html_url": "https://github.com/IshanA2007/theCourseForum2/pull/5",
        "number": 5,
    }
    methods_paths = [f"{m} {p}" for m, p in calls]
    # the required sequence: token -> base ref -> create branch -> file -> PR
    assert any(p.endswith("/access_tokens") and m == "POST" for m, p in calls)
    assert any("/git/ref/heads/master" in p for _, p in calls)
    assert any(p.endswith("/git/refs") and m == "POST" for m, p in calls)
    assert any("/contents/postmortems/" in p and m == "PUT" for m, p in calls)
    assert any(p.endswith("/pulls") and m == "POST" for m, p in calls)
    assert not any("/merge" in p for _, p in calls)  # never merges
    assert methods_paths  # sanity


async def test_writer_base64_encodes_file_content():
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/access_tokens"):
            return httpx.Response(201, json={"token": "t"})
        if "/git/ref/heads/" in path:
            return httpx.Response(200, json={"object": {"sha": "s"}})
        if path.endswith("/git/refs"):
            return httpx.Response(201, json={})
        if "/contents/" in path:
            captured["content"] = request.read()
            return httpx.Response(201, json={})
        if path.endswith("/pulls"):
            return httpx.Response(201, json={"html_url": "u", "number": 1})
        return httpx.Response(404)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    writer = GitHubAppWriter("a", _rsa_pem(), "i", _REPO, client=client)
    try:
        await writer.open_postmortem_pr(**_pr_kwargs())
    finally:
        await client.aclose()

    import json as _json

    sent = _json.loads(captured["content"])
    assert base64.b64decode(sent["content"]).decode() == "# Postmortem\n\nbody text\n"
    assert sent["branch"] == "culprit/postmortem-7-fielderror"
