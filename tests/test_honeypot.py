from __future__ import annotations

import asyncio
from types import SimpleNamespace

import discord

from cogs.honeypot import Honeypot
from utils.settings import update_guild_settings


def _post(storage, monkeypatch, *, channel_id=7, bot=False, mod=False, top_role=1, owner=False):
    monkeypatch.setattr(discord, "Member", SimpleNamespace)
    done, logged = [], []

    async def ban(member, reason=None, delete_message_seconds=0):
        done.append(("ban", delete_message_seconds))

    async def unban(member, reason=None):
        done.append(("unban",))

    async def kick(reason=None):
        done.append(("kick",))

    async def delete():
        done.append(("delete",))

    async def log(guild, **kwargs):
        logged.append((kwargs["event"], kwargs["title"]))

    guild = SimpleNamespace(id=1, owner_id=5 if owner else 999, ban=ban, unban=unban, me=SimpleNamespace(top_role=10))
    author = SimpleNamespace(id=5, bot=bot, top_role=top_role, kick=kick,
                             guild_permissions=SimpleNamespace(administrator=False, manage_messages=mod, ban_members=False))
    message = SimpleNamespace(guild=guild, author=author, channel=SimpleNamespace(id=channel_id, name="trap"), delete=delete)
    moderation = SimpleNamespace(_log_mod_action=log)
    cog = Honeypot(SimpleNamespace(storage_path=storage, user=SimpleNamespace(id=42), embeds=SimpleNamespace(error_color=None), get_cog=lambda name: moderation))
    asyncio.run(cog.on_message(message))
    return done, logged


def test_posting_in_the_honeypot_bans_and_cleans_up(storage, monkeypatch):
    update_guild_settings(storage, 1, {"honeypot_channel_id": "7"})
    done, logged = _post(storage, monkeypatch)
    assert done == [("delete",), ("ban", 86400)] and logged == [("ban", "🍯 Honeypot Ban")]


def test_softban_and_kick_actions(storage, monkeypatch):
    update_guild_settings(storage, 1, {"honeypot_channel_id": "7", "honeypot_action": "softban"})
    assert _post(storage, monkeypatch)[0] == [("delete",), ("ban", 86400), ("unban",)]
    update_guild_settings(storage, 1, {"honeypot_action": "kick"})
    done, logged = _post(storage, monkeypatch)
    assert done == [("delete",), ("kick",)] and logged == [("kick", "🍯 Honeypot Kick")]


def test_other_channels_mods_bots_the_owner_and_an_unset_honeypot_are_safe(storage, monkeypatch):
    assert _post(storage, monkeypatch)[0] == []  # no honeypot configured
    update_guild_settings(storage, 1, {"honeypot_channel_id": "7"})
    for kwargs in ({"channel_id": 8}, {"mod": True}, {"bot": True}, {"owner": True}):
        assert _post(storage, monkeypatch, **kwargs)[0] == [], kwargs
    # Someone the bot can't act on has the message removed, and nothing else is claimed.
    done, logged = _post(storage, monkeypatch, top_role=10)
    assert done == [("delete",)] and logged == []


def test_exempt_users_and_roles_are_left_alone(storage, monkeypatch):
    update_guild_settings(storage, 1, {"honeypot_channel_id": "7", "honeypot_exempt_users": [5]})
    assert _post(storage, monkeypatch)[0] == []
    update_guild_settings(storage, 1, {"honeypot_exempt_users": [6], "honeypot_exempt_roles": [30]})
    assert _post(storage, monkeypatch)[0] == [("delete",), ("ban", 86400)]


def test_a_switched_off_honeypot_does_nothing(storage, monkeypatch):
    update_guild_settings(storage, 1, {"honeypot_channel_id": "7"})
    update_guild_settings(storage, 1, {"honeypot_enabled": False})
    assert _post(storage, monkeypatch)[0] == []


def test_the_notice_counts_catches_and_is_reposted_if_deleted(storage):
    from cogs.honeypot import clean_channel_name, notice_text
    from utils import honeypot as state

    assert "**0** accounts banned" in notice_text("ban", 0) and "**1** account kicked" in notice_text("kick", 1)
    assert clean_channel_name("  Do Not Post  ") == "do-not-post" and clean_channel_name("") == "do-not-post-here"

    sent, edits, deleted = [], [], []

    class Partial:
        def __init__(self, message_id, missing):
            self.id, self.missing = message_id, missing

        async def edit(self, embed=None):
            if self.missing:
                raise discord.NotFound(SimpleNamespace(status=404, reason="x"), "gone")
            edits.append((self.id, embed))

        async def delete(self):
            deleted.append(self.id)

    class Channel:
        id, name, missing = 7, "trap", False

        async def send(self, embed=None):
            sent.append(embed)
            return SimpleNamespace(id=100 + len(sent))

        def get_partial_message(self, message_id):
            return Partial(message_id, self.missing)

    channel = Channel()
    guild = SimpleNamespace(id=1, get_channel={7: channel}.get)
    channel.guild = guild
    embeds = SimpleNamespace(error_embed=lambda title, text: text)
    cog = Honeypot(SimpleNamespace(storage_path=storage, embeds=embeds))
    asyncio.run(cog.refresh_notice(guild, "ban"))
    assert sent == [] and edits == []  # no notice was ever posted: nothing to update
    asyncio.run(cog.post_notice(channel, "ban"))
    assert "**0** accounts" in sent[0] and state.get_state(storage, 1)["message_id"] == 101
    assert state.record_catch(storage, 1) == 1 and state.record_catch(storage, 1) == 2
    asyncio.run(cog.refresh_notice(guild, "ban"))
    assert edits[0][0] == 101 and "**2** accounts banned" in edits[0][1]
    channel.missing = True  # someone deleted the notice
    asyncio.run(cog.refresh_notice(guild, "ban"))
    assert len(sent) == 2 and "**2** accounts" in sent[1] and state.get_state(storage, 1) == {"guild_id": 1, "channel_id": 7, "message_id": 102, "caught": 2}
    assert deleted == [101]


def test_creating_a_channel_turns_the_honeypot_on(storage):
    from utils.settings import get_guild_settings

    class Channel:
        id, name = 55, "do-not-post-here"

        async def send(self, embed=None):
            return SimpleNamespace(id=9)

    created = {}

    async def create_text_channel(name, topic=None, position=None, reason=None):
        created.update(name=name, position=position)
        channel = Channel()
        channel.guild = guild
        return channel

    guild = SimpleNamespace(id=1, create_text_channel=create_text_channel, get_channel=lambda cid: None)
    cog = Honeypot(SimpleNamespace(storage_path=storage, embeds=SimpleNamespace(error_embed=lambda title, text: text)))
    asyncio.run(cog.create_channel(guild, "Bot Trap", "an admin"))
    settings = get_guild_settings(storage, 1)
    assert created == {"name": "bot-trap", "position": 0}
    assert settings["honeypot_channel_id"] == "55" and settings["honeypot_enabled"] is True
