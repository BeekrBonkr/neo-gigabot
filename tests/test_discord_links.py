from __future__ import annotations

import pytest

from utils.discord_links import parse_message_link


@pytest.mark.parametrize(
    "url",
    [
        "https://discord.com/channels/123/456/789",
        "https://discordapp.com/channels/123/456/789",
        "https://canary.discord.com/channels/123/456/789",
        "https://ptb.discord.com/channels/123/456/789",
        "http://discord.com/channels/123/456/789",
        "  https://discord.com/channels/123/456/789  ",
    ],
)
def test_parse_guild_message_link_variants(url: str) -> None:
    assert parse_message_link(url) == ("123", 456, 789)


def test_parse_dm_message_link() -> None:
    guild_id, channel_id, message_id = parse_message_link("https://discord.com/channels/@me/456/789")
    assert guild_id == "@me"
    assert channel_id == 456
    assert message_id == 789


@pytest.mark.parametrize(
    "url",
    [
        "",
        "not a link",
        "https://example.com/channels/123/456/789",
        "https://discord.com/channels/123/456",
        "https://discord.com/channels/123/456/789/extra",
        "https://discord.com/channels/abc/456/789",
        "https://discord.com/channels/123/456/789?query=1",
    ],
)
def test_parse_invalid_message_links(url: str) -> None:
    with pytest.raises(ValueError):
        parse_message_link(url)


def _server(readable_by: set[int], members: set[int]):
    """A channel in a server, with a bot that can read anything in it."""
    import asyncio  # noqa: F401
    from types import SimpleNamespace as NS

    import discord

    fetched = []

    class Channel:
        id = 456

        def permissions_for(self, member):
            return discord.Permissions(view_channel=member.id in readable_by, read_message_history=member.id in readable_by)

        async def fetch_message(self, message_id):
            fetched.append(message_id)
            return NS(id=message_id, content="private staff discussion")

    channel = Channel()

    async def fetch_member(user_id):
        raise discord.NotFound(NS(status=404, reason="Not Found"), "Unknown Member")

    guild = NS(id=123, get_channel=lambda cid: channel, get_thread=lambda cid: None,
               get_member=lambda uid: NS(id=uid) if uid in members else None, fetch_member=fetch_member)
    channel.guild = guild
    bot = NS(get_guild=lambda gid: guild)
    return bot, fetched


def test_a_message_link_is_only_followed_for_someone_who_can_read_that_channel() -> None:
    import asyncio
    from types import SimpleNamespace as NS

    from utils.discord_links import fetch_message_from_link

    link = "https://discord.com/channels/123/456/789"
    bot, fetched = _server(readable_by={1}, members={1, 2})
    assert asyncio.run(fetch_message_from_link(bot, link, viewer=NS(id=1))).id == 789
    for outsider in (2, 3):  # a member who cannot see the channel, and someone not in the server at all
        with pytest.raises(ValueError, match="that you can see that channel"):
            asyncio.run(fetch_message_from_link(bot, link, viewer=NS(id=outsider)))
    assert fetched == [789]


def test_a_dm_link_is_only_followed_for_the_person_the_dm_is_with() -> None:
    import asyncio
    from types import SimpleNamespace as NS

    from utils.discord_links import can_read

    dm = NS(recipient=NS(id=5))
    assert asyncio.run(can_read(dm, NS(id=5))) is True
    assert asyncio.run(can_read(dm, NS(id=6))) is False
