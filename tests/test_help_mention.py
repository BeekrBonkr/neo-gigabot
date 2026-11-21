"""The bare-mention help reply must never raise, even when the bot can neither
DM the user nor post in the channel it was mentioned in (seen in production as
an unhandled Forbidden in on_message)."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import discord

from cogs.help import Help


def _forbidden(code: int) -> discord.Forbidden:
    response = SimpleNamespace(status=403, reason="Forbidden")
    return discord.Forbidden(response, {"message": "nope", "code": code})


def _make_cog() -> Help:
    cog = Help.__new__(Help)
    cog._mention_cooldowns = {}
    cog.build_home_embed = lambda: discord.Embed(title="help")  # type: ignore[method-assign]
    cog.new_commands = lambda: []  # type: ignore[method-assign]
    return cog


def _message(*, dm_error: Exception | None, channel_error: Exception | None) -> tuple[SimpleNamespace, list[str]]:
    sends: list[str] = []

    async def channel_send(content=None, **kwargs):
        if channel_error is not None:
            raise channel_error
        sends.append(f"channel:{content or kwargs.get('embed').title}")
        return SimpleNamespace(id=1)

    async def dm_send(**kwargs):
        if dm_error is not None:
            raise dm_error
        sends.append("dm")
        return SimpleNamespace(id=2)

    async def create_dm():
        return SimpleNamespace(send=dm_send)

    message = SimpleNamespace(
        author=SimpleNamespace(id=42, mention="<@42>", create_dm=create_dm),
        guild=SimpleNamespace(id=1),
        channel=SimpleNamespace(send=channel_send),
    )
    return message, sends


def test_dm_closed_falls_back_to_channel() -> None:
    cog = _make_cog()
    message, sends = _message(dm_error=_forbidden(50007), channel_error=None)
    asyncio.run(cog._send_help_via_message(message))
    assert len(sends) == 1 and sends[0].startswith("channel:*(Your DMs look closed")


def test_dm_closed_and_channel_unwritable_is_silent() -> None:
    cog = _make_cog()
    message, sends = _message(dm_error=_forbidden(50007), channel_error=_forbidden(50013))
    asyncio.run(cog._send_help_via_message(message))  # must not raise
    assert sends == []


def test_dm_ok_but_channel_notice_unwritable_is_silent() -> None:
    cog = _make_cog()
    message, sends = _message(dm_error=None, channel_error=_forbidden(50013))
    asyncio.run(cog._send_help_via_message(message))
    assert sends == ["dm"]
