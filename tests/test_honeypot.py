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
