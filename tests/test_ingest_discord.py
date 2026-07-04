"""Task 2 — Discord interaction parsing + the /discord/interactions route.

A signed ``/resolve`` slash command resolves an incident through the shared
resolver (``source="discord"``); a PING gets a PONG; a bad signature is a 401.
The route verify path uses a keypair generated in-test (a real Ed25519 signature,
no mock).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from culprit.config import get_settings
from culprit.ingest.discord import interaction_type, parse_resolve_incident_id
from culprit.models import Incident


def _keypair() -> tuple[Ed25519PrivateKey, str]:
    priv = Ed25519PrivateKey.generate()
    return priv, priv.public_key().public_bytes_raw().hex()


def _resolve_interaction(incident_id: int) -> dict:
    return {
        "type": 2,
        "data": {
            "name": "resolve",
            "options": [{"name": "incident_id", "type": 4, "value": incident_id}],
        },
    }


# --- pure parsing ------------------------------------------------------------


def test_parse_resolve_extracts_incident_id():
    assert parse_resolve_incident_id(_resolve_interaction(42)) == 42


def test_parse_non_resolve_command_returns_none():
    assert parse_resolve_incident_id({"type": 2, "data": {"name": "other"}}) is None


def test_interaction_type():
    assert interaction_type({"type": 1}) == 1
    assert interaction_type({"type": 2}) == 2


# --- the route ---------------------------------------------------------------


async def test_route_ping_returns_pong(client, monkeypatch):
    priv, pub_hex = _keypair()
    monkeypatch.setenv("DISCORD_PUBLIC_KEY", pub_hex)
    get_settings.cache_clear()
    try:
        body = json.dumps({"type": 1}).encode()
        ts = "1700000000"
        sig = priv.sign(ts.encode() + body).hex()
        resp = await client.post(
            "/discord/interactions",
            content=body,
            headers={"x-signature-ed25519": sig, "x-signature-timestamp": ts},
        )
        assert resp.status_code == 200
        assert resp.json() == {"type": 1}  # PONG
    finally:
        get_settings.cache_clear()


async def test_route_resolve_command_resolves_the_incident(
    client, db_session, monkeypatch
):
    priv, pub_hex = _keypair()
    monkeypatch.setenv("DISCORD_PUBLIC_KEY", pub_hex)
    get_settings.cache_clear()
    try:
        inc = Incident(
            opened_at=datetime(2026, 7, 4, 4, 0, tzinfo=UTC),
            status="open",
            correlation_key="Boom",
            severity=1,
        )
        db_session.add(inc)
        await db_session.commit()

        body = json.dumps(_resolve_interaction(inc.id)).encode()
        ts = "1700000001"
        sig = priv.sign(ts.encode() + body).hex()
        resp = await client.post(
            "/discord/interactions",
            content=body,
            headers={"x-signature-ed25519": sig, "x-signature-timestamp": ts},
        )
        assert resp.status_code == 200
        assert resp.json()["type"] == 4  # CHANNEL_MESSAGE_WITH_SOURCE
        await db_session.refresh(inc)
        assert inc.status == "resolved"
        assert inc.resolution_source == "discord"
    finally:
        get_settings.cache_clear()


async def test_route_bad_signature_is_401(client, monkeypatch):
    _, pub_hex = _keypair()
    monkeypatch.setenv("DISCORD_PUBLIC_KEY", pub_hex)
    get_settings.cache_clear()
    try:
        resp = await client.post(
            "/discord/interactions",
            content=b'{"type":1}',
            headers={"x-signature-ed25519": "00" * 64, "x-signature-timestamp": "1"},
        )
        assert resp.status_code == 401
    finally:
        get_settings.cache_clear()
