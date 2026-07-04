"""Parse Discord interactions — the ``/resolve`` slash command (M4 decision 2).

A signed interaction reaches ``POST /discord/interactions`` (verified in
``culprit/discord_verify.py``). Discord's interaction ``type`` is 1 for a PING
(answered with a PONG) and 2 for an application command. The ``/resolve`` command
carries an ``incident_id`` option; this module extracts it. It never touches the
DB — the route resolves via the shared ``resolve_incident``.
"""

from __future__ import annotations

# Discord interaction + response type numbers (from the interactions API).
PING = 1
APPLICATION_COMMAND = 2
PONG = 1
CHANNEL_MESSAGE_WITH_SOURCE = 4


def interaction_type(interaction: dict) -> int | None:
    """The interaction ``type`` (1 = PING, 2 = application command)."""
    return interaction.get("type")


def parse_resolve_incident_id(interaction: dict) -> int | None:
    """The incident id from a ``/resolve`` command, or None if it isn't one."""
    data = interaction.get("data") or {}
    if data.get("name") != "resolve":
        return None
    for opt in data.get("options") or []:
        if opt.get("name") == "incident_id":
            try:
                return int(opt.get("value"))
            except (TypeError, ValueError):
                return None
    return None
