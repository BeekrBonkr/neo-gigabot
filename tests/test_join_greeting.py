"""The greeting sent when the bot is added to a server: a DM to whoever added
it, falling back to the server's main channel."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from types import SimpleNamespace

import discord
import pytest

import cogs.settings as settings_cog
from cogs.settings import Settings
from utils.settings import create_guild_settings, guild_settings_exists

BOT_ID = 99


class _Sink:
    """Anything that can be sent to: a user or a channel."""

    def __init__(self, *, fails: bool = False, **attrs) -> None:
        self.fails, self.sent = fails, []
        self.__dict__.update(attrs)

    async def send(self, *, embed):
        if self.fails:
            raise discord.Forbidden(SimpleNamespace(status=403, reason="Forbidden"), "Cannot send messages to this user")
        self.sent.append(embed.description)


def _channel(name: str, position: int, *, can_send: bool = True, fails: bool = False) -> _Sink:
    perms = SimpleNamespace(view_channel=True, send_messages=can_send, embed_links=True)
    return _Sink(fails=fails, name=name, position=position, id=position, permissions_for=lambda me: perms)


def _guild(*, entries=(), channels=(), system=None, audit=True, joined_ago=5):
    me = SimpleNamespace(
        id=BOT_ID,
        guild_permissions=SimpleNamespace(view_audit_log=audit),
        joined_at=discord.utils.utcnow() - timedelta(seconds=joined_ago),
    )

    async def audit_logs(**kwargs):
        for entry in entries:
            yield entry

    return SimpleNamespace(id=1234, name="Test Server", me=me, system_channel=system, text_channels=list(channels), audit_logs=audit_logs)


def _added_by(user, target_id=BOT_ID):
    return SimpleNamespace(user=user, target=SimpleNamespace(id=target_id))


def _cog(storage, website="https://example.com") -> Settings:
    embeds = SimpleNamespace(info_embed=lambda title, description: SimpleNamespace(title=title, description=description))
    bot = SimpleNamespace(
        storage_path=storage, user=SimpleNamespace(name="Bot"), embeds=embeds, config=SimpleNamespace(website_url=website)
    )
    return Settings(bot)


@pytest.fixture(autouse=True)
def _no_waiting(monkeypatch):
    monkeypatch.setattr(settings_cog, "INVITER_LOOKUP_RETRY", 0)


def _join(cog, guild):
    asyncio.run(cog.on_guild_join(guild))


def test_the_person_who_added_the_bot_gets_a_dm_with_the_settings_page(storage):
    inviter, general = _Sink(), _channel("general", 0)
    other_bot = _added_by(_Sink(), target_id=5)
    guild = _guild(entries=[other_bot, _added_by(inviter)], channels=[general], system=general)
    _join(_cog(storage), guild)
    assert guild_settings_exists(storage, guild.id)
    assert len(inviter.sent) == 1 and "https://example.com/app/1234/settings" in inviter.sent[0]
    assert "Test Server" in inviter.sent[0]
    assert general.sent == [] and other_bot.user.sent == []


def test_a_closed_dm_falls_back_to_the_system_channel(storage):
    inviter, system = _Sink(fails=True), _channel("lobby", 3)
    guild = _guild(entries=[_added_by(inviter)], channels=[_channel("general", 0), system], system=system)
    _join(_cog(storage), guild)
    assert len(system.sent) == 1 and "https://example.com/app/1234/settings" in system.sent[0]


def test_an_unknown_inviter_falls_back_to_the_channel(storage):
    for guild in (_guild(audit=False, system=_channel("lobby", 0)), _guild(entries=[], system=_channel("lobby", 0))):
        _join(_cog(storage / str(id(guild))), guild)
        assert len(guild.system_channel.sent) == 1


def test_main_channel_prefers_general_then_the_topmost_usable_channel(storage):
    locked, top, general = _channel("rules", 0, can_send=False), _channel("chat", 1), _channel("General", 2)
    assert Settings._main_channel(_guild(channels=[general, top, locked])) is general
    assert Settings._main_channel(_guild(channels=[top, locked])) is top
    assert Settings._main_channel(_guild(channels=[general, top], system=locked)) is general
    assert Settings._main_channel(_guild(channels=[locked])) is None


def test_nothing_to_send_to_is_not_an_error(storage):
    _join(_cog(storage), _guild(entries=[_added_by(_Sink(fails=True))], channels=[_channel("general", 0, fails=True)]))


def test_a_returning_server_is_greeted_only_when_the_bot_really_just_joined(storage):
    cog = _cog(storage)
    create_guild_settings(storage, 1234)
    replayed = _Sink()
    _join(cog, _guild(entries=[_added_by(replayed)], joined_ago=86400))
    assert replayed.sent == []
    readded = _Sink()
    _join(cog, _guild(entries=[_added_by(readded)], joined_ago=5))
    assert len(readded.sent) == 1


def test_without_a_website_the_greeting_points_at_the_settings_command(storage):
    inviter = _Sink()
    _join(_cog(storage, website=""), _guild(entries=[_added_by(inviter)]))
    assert "`/settings`" in inviter.sent[0] and "http" not in inviter.sent[0]
