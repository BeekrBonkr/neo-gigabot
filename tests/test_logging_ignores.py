from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import discord
import pytest

from cogs.logging import Logging
from utils.settings import update_guild_settings

GUILD_ID = 1111
LOG_CHANNEL_ID = 2222
IGNORED_USER_ID = 3333
IGNORED_CHANNEL_ID = 4444
NORMAL_USER_ID = 5555


class _FakeLogChannel(discord.TextChannel):
    """Just enough of a TextChannel to pass the isinstance gate in _log and
    record what would have been sent."""

    def __init__(self) -> None:  # deliberately skip TextChannel.__init__
        self.sent: list[discord.Embed] = []

    async def send(self, embed: discord.Embed | None = None, **kwargs) -> None:
        if embed is not None:
            self.sent.append(embed)


@pytest.fixture()
def log_channel() -> _FakeLogChannel:
    return _FakeLogChannel()


@pytest.fixture()
def guild(log_channel: _FakeLogChannel) -> MagicMock:
    guild = MagicMock()
    guild.id = GUILD_ID
    guild.get_channel = lambda cid: log_channel if cid == LOG_CHANNEL_ID else None
    guild.me = None  # skips audit-log lookups
    return guild


@pytest.fixture()
def cog(storage: Path) -> Logging:
    update_guild_settings(
        storage,
        GUILD_ID,
        {
            "logging_enabled": True,
            "logging_channel_id": str(LOG_CHANNEL_ID),
            "ignored_users": [IGNORED_USER_ID],
            "ignored_log_channels": [IGNORED_CHANNEL_ID],
        },
    )
    bot = MagicMock()
    bot.storage_path = storage
    bot.embeds.create = lambda **kw: discord.Embed(
        title=kw.get("title"), description=kw.get("description"), color=kw.get("color")
    )
    return Logging(bot)


def _user(user_id: int) -> MagicMock:
    user = MagicMock()
    user.id = user_id
    user.bot = False
    user.mention = f"<@{user_id}>"
    user.display_avatar.url = "https://example.invalid/avatar.png"
    return user


def _message(author: MagicMock, guild: MagicMock, channel_id: int, parent_id: int | None = None) -> MagicMock:
    message = MagicMock()
    message.author = author
    message.guild = guild
    message.channel = MagicMock()
    message.channel.id = channel_id
    message.channel.mention = f"<#{channel_id}>"
    if parent_id is None:
        # Plain text channels have no parent_id attribute at all.
        del message.channel.parent_id
    else:
        message.channel.parent_id = parent_id
    message.content = "hello"
    message.attachments = []
    message.id = 42
    return message


def test_message_delete_by_ignored_user_is_skipped(cog: Logging, guild: MagicMock, log_channel: _FakeLogChannel) -> None:
    asyncio.run(cog.on_message_delete(_message(_user(IGNORED_USER_ID), guild, channel_id=99)))
    assert log_channel.sent == []


def test_message_delete_by_normal_user_is_logged(cog: Logging, guild: MagicMock, log_channel: _FakeLogChannel) -> None:
    asyncio.run(cog.on_message_delete(_message(_user(NORMAL_USER_ID), guild, channel_id=99)))
    assert len(log_channel.sent) == 1


def test_message_delete_in_thread_of_ignored_channel_is_skipped(
    cog: Logging, guild: MagicMock, log_channel: _FakeLogChannel
) -> None:
    message = _message(_user(NORMAL_USER_ID), guild, channel_id=99, parent_id=IGNORED_CHANNEL_ID)
    asyncio.run(cog.on_message_delete(message))
    assert log_channel.sent == []


def test_thread_create_by_ignored_user_is_skipped(cog: Logging, guild: MagicMock, log_channel: _FakeLogChannel) -> None:
    thread = MagicMock()
    thread.guild = guild
    thread.owner_id = IGNORED_USER_ID
    thread.parent_id = 99
    thread.parent = MagicMock()
    thread.parent.mention = "<#99>"
    thread.mention = "<#7777>"
    thread.id = 7777
    asyncio.run(cog.on_thread_create(thread))
    assert log_channel.sent == []


def test_invite_create_by_ignored_user_is_skipped(cog: Logging, guild: MagicMock, log_channel: _FakeLogChannel) -> None:
    invite = MagicMock(spec=discord.Invite)
    invite_guild = MagicMock(spec=discord.Guild)
    invite_guild.id = GUILD_ID
    invite_guild.get_channel = guild.get_channel
    invite.guild = invite_guild
    invite.inviter = _user(IGNORED_USER_ID)
    invite.channel = MagicMock()
    invite.channel.id = 99
    invite.channel.mention = "<#99>"
    invite.code = "abc"
    invite.max_uses = 0
    invite.expires_at = None
    asyncio.run(cog.on_invite_create(invite))
    assert log_channel.sent == []


def test_voice_join_by_ignored_user_is_skipped(cog: Logging, guild: MagicMock, log_channel: _FakeLogChannel) -> None:
    member = _user(IGNORED_USER_ID)
    member.guild = guild
    voice_channel = MagicMock()
    voice_channel.id = 6666
    voice_channel.mention = "<#6666>"
    asyncio.run(
        cog.on_voice_state_update(member, SimpleNamespace(channel=None), SimpleNamespace(channel=voice_channel))
    )
    assert log_channel.sent == []


def test_tree_check_blocks_disabled_command(storage: Path) -> None:
    from unittest.mock import AsyncMock

    from main import GigaBot

    update_guild_settings(storage, GUILD_ID, {"blocked_commands": ["meme"]})

    stub = MagicMock()
    stub.storage_path = storage
    stub.embeds.error_interaction = AsyncMock()

    def _interaction(command_name: str, qualified: str | None = None) -> MagicMock:
        interaction = MagicMock()
        interaction.type = discord.InteractionType.application_command
        interaction.guild = MagicMock()
        interaction.guild.id = GUILD_ID
        interaction.command = MagicMock()
        interaction.command.name = command_name
        interaction.command.qualified_name = qualified or command_name
        return interaction

    assert asyncio.run(GigaBot.on_tree_interaction_check(stub, _interaction("meme"))) is False
    stub.embeds.error_interaction.assert_awaited_once()

    assert asyncio.run(GigaBot.on_tree_interaction_check(stub, _interaction("ping"))) is True
    # /settings can never be locked out, even if it somehow lands in the list
    update_guild_settings(storage, GUILD_ID, {"blocked_commands": ["settings"]})
    assert asyncio.run(GigaBot.on_tree_interaction_check(stub, _interaction("settings"))) is True
