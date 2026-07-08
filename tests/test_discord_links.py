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
