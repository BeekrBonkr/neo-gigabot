from __future__ import annotations

import asyncio
from types import SimpleNamespace

from cogs.tempvoice import TempVoice, channel_name
from utils import tempvoice as store
from utils.settings import update_guild_settings


def test_channel_names_come_from_the_template_and_fit_discords_limit():
    member = SimpleNamespace(display_name="Robin", name="robin")
    assert channel_name("{name}'s channel", member) == "Robin's channel" and channel_name("🎧 {user}", member) == "🎧 robin"
    assert channel_name("", member) == "Robin's channel" and channel_name("   ", member) == "Robin's channel"
    assert len(channel_name("x" * 500, member)) == 100


class Person:
    pass


def _world(storage):
    channels, created, deleted = {}, [], []

    class Channel:
        def __init__(self, cid, category=None):
            self.id, self.category, self.members = cid, category, []
            channels[cid] = self

        async def delete(self, reason=None):
            deleted.append(self.id)
            channels.pop(self.id, None)

    async def create_voice_channel(name, category=None, overwrites=None, user_limit=0, reason=None):
        channel = Channel(100 + len(created), category)
        created.append((name, user_limit, category))
        return channel

    guild = SimpleNamespace(id=1, me=object(), get_channel=channels.get, create_voice_channel=create_voice_channel)
    hub = Channel(50, category="Voice")

    def member(uid, name="Robin", bot=False, can_move=True):
        async def move_to(channel, reason=None):
            if not can_move:
                import discord
                raise discord.HTTPException(SimpleNamespace(status=400, reason="x"), "not connected")
            me.channel = channel
            channel.members.append(me)

        me = Person()  # hashable, because the member is a key in the channel's permission overwrites
        me.__dict__.update(id=uid, bot=bot, display_name=name, name=name.lower(), guild=guild, move_to=move_to, channel=None)
        return me

    cog = TempVoice(SimpleNamespace(storage_path=storage, get_guild=lambda gid: guild if gid == 1 else None))
    update_guild_settings(storage, 1, {"tempvoice_hub_channel_id": "50", "tempvoice_user_limit": 4})
    state = lambda channel: SimpleNamespace(channel=channel)  # noqa: E731
    return cog, hub, member, state, channels, created, deleted


def test_joining_the_hub_creates_a_channel_and_emptying_it_deletes_it(storage):
    cog, hub, member, state, channels, created, deleted = _world(storage)
    robin = member(5)
    asyncio.run(cog.on_voice_state_update(robin, state(None), state(hub)))
    assert created == [("Robin's channel", 4, "Voice")] and robin.channel.id == 100 and store.get(storage, 100)["owner_id"] == 5
    asyncio.run(cog.on_voice_state_update(robin, state(hub), state(robin.channel)))   # the move itself: nothing new
    assert len(created) == 1 and deleted == []
    mine = robin.channel
    mine.members.clear()
    asyncio.run(cog.on_voice_state_update(robin, state(mine), state(None)))
    assert deleted == [100] and store.get(storage, 100) is None


def test_one_channel_per_member_bots_and_other_channels_are_ignored(storage):
    cog, hub, member, state, channels, created, deleted = _world(storage)
    robin = member(5)
    asyncio.run(cog.on_voice_state_update(robin, state(None), state(hub)))
    asyncio.run(cog.on_voice_state_update(robin, state(robin.channel), state(hub)))   # back in the hub: sent to the channel they have
    assert len(created) == 1 and robin.channel.id == 100
    asyncio.run(cog.on_voice_state_update(member(6, bot=True), state(None), state(hub)))
    other = SimpleNamespace(id=60, members=[], category=None)
    asyncio.run(cog.on_voice_state_update(member(7), state(None), state(other)))
    asyncio.run(cog.on_voice_state_update(member(7), state(other), state(None)))      # an ordinary empty channel is never deleted
    assert len(created) == 1 and deleted == []


def test_a_channel_nobody_could_be_moved_into_is_cleaned_up_and_restarts_sweep_empties(storage):
    cog, hub, member, state, channels, created, deleted = _world(storage)
    asyncio.run(cog.on_voice_state_update(member(8, can_move=False), state(None), state(hub)))
    assert deleted == [100] and store.all_channels(storage) == []
    robin = member(5)
    asyncio.run(cog.on_voice_state_update(robin, state(None), state(hub)))
    robin.channel.members.clear()                       # emptied while the bot was down
    store.add(storage, 999, 1, 5)                       # and one whose channel is already gone
    asyncio.run(cog.on_ready())
    assert 101 in deleted and store.all_channels(storage) == []
