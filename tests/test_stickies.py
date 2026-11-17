from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from cogs.stickies import Stickies
from utils import stickies as store


def test_a_sticky_comes_back_after_15_seconds_or_5_messages():
    pacer = store.Pacer()
    pacer.posted(1, now=0)
    assert [pacer.should_post(1, False, now=t) for t in (1, 2, 3, 4)] == [False] * 4
    assert pacer.should_post(1, False, now=5) is True                       # the fifth message
    assert pacer.should_post(1, False, now=6) is False and pacer.should_post(1, False, now=21) is True  # 15 seconds on
    pacer.posted(2, now=0)
    assert [pacer.should_post(2, True, now=t) for t in range(1, 13)] == [False] * 12
    assert pacer.should_post(2, True, now=13) is True                       # slow: the thirteenth message
    assert pacer.should_post(2, True, now=40) is False and pacer.should_post(2, True, now=49) is True   # or 35 seconds
    assert store.Pacer().should_post(3, False, now=0) is True               # never posted yet


def test_one_sticky_per_channel_with_stop_start_and_remove(storage):
    store.set_sticky(storage, 1, 7, "  read the rules\\nplease ", 9)
    assert store.get(storage, 7)["content"] == "read the rules\nplease" and store.active_channels(storage) == {7}
    store.set_last_message(storage, 7, 555)
    edited = store.set_sticky(storage, 1, 7, "new text", 9, embed=True, slow=True)
    assert (edited["content"], edited["embed"], edited["slow"], edited["last_message_id"]) == ("new text", 1, 1, 555)
    assert store.set_active(storage, 7, False)["active"] == 0 and store.active_channels(storage) == set()
    assert store.set_active(storage, 7, True)["active"] == 1 and store.active_channels(storage) == {7}
    assert store.set_active(storage, 8, True) is None
    assert [s["channel_id"] for s in store.list_for_guild(storage, 1)] == [7] and store.list_for_guild(storage, 2) == []
    assert store.remove(storage, 7)["content"] == "new text" and store.remove(storage, 7) is None and store.active_channels(storage) == set()
    for bad in ("", "   ", "x" * 1901):
        with pytest.raises(store.StickyError):
            store.set_sticky(storage, 1, 7, bad, 9)


def test_reposting_deletes_the_old_copy_and_ignores_the_bots_own_messages(storage):
    sent, deleted = [], []

    class Channel:
        id = 7

        def get_partial_message(self, message_id):
            async def delete():
                deleted.append(message_id)
            return SimpleNamespace(delete=delete)

        async def send(self, content=None, **kwargs):
            sent.append(content)
            return SimpleNamespace(id=1000 + len(sent))

    channel = Channel()
    cog = Stickies(SimpleNamespace(storage_path=storage, user=SimpleNamespace(id=42), embeds=None))
    store.set_sticky(storage, 1, 7, "read the rules", 9)
    message = lambda author, cid=7: SimpleNamespace(guild=object(), channel=channel if cid == 7 else SimpleNamespace(id=cid), author=SimpleNamespace(id=author))  # noqa: E731
    asyncio.run(cog.on_message(message(5)))
    assert sent == [store.HEADER + "read the rules"] and deleted == [] and store.get(storage, 7)["last_message_id"] == 1001
    for _ in range(4):
        asyncio.run(cog.on_message(message(5)))
    assert len(sent) == 1                                   # too soon and too few messages
    asyncio.run(cog.on_message(message(5)))
    assert len(sent) == 2 and deleted == [1001]             # fifth message: old copy removed, new one posted
    for _ in range(10):
        asyncio.run(cog.on_message(message(42)))            # the bot's own messages never count
    asyncio.run(cog.on_message(message(5, cid=8)))          # other channels are untouched
    assert len(sent) == 2
    store.set_active(storage, 7, False)
    for _ in range(10):
        asyncio.run(cog.on_message(message(5)))
    assert len(sent) == 2


def test_the_website_can_manage_stickies_only_in_its_own_server(storage):
    from aiohttp.test_utils import TestClient, TestServer

    from cogs.panel_bridge import PanelBridge

    posted, deleted = [], []

    class Channel:
        def __init__(self, cid, can_send=True):
            self.id, self.name, self.can_send = cid, f"chan{cid}", can_send

        def permissions_for(self, me):
            return SimpleNamespace(view_channel=True, send_messages=self.can_send)

        async def send(self, *a, **k):
            return SimpleNamespace(id=1)

        def get_partial_message(self, message_id):
            async def delete():
                deleted.append(message_id)
            return SimpleNamespace(delete=delete)

    async def post(channel, sticky):
        posted.append((channel.id, sticky["content"], sticky["active"]))

    channels = {7: Channel(7), 8: Channel(8, can_send=False)}
    admin = SimpleNamespace(id=4, guild_permissions=SimpleNamespace(administrator=True))
    member = SimpleNamespace(id=5, guild_permissions=SimpleNamespace(administrator=False))
    guild = SimpleNamespace(id=1, owner_id=99, me=object(), get_channel=channels.get, get_member={4: admin, 5: member}.get)
    cog = SimpleNamespace(post=post, pacer=store.Pacer())
    bot = SimpleNamespace(storage_path=storage, guilds=[guild], voice_clients=[], get_guild=lambda gid: guild if gid == 1 else None,
                          get_cog=lambda name: cog if name == "Stickies" else None)
    bridge = PanelBridge(bot)
    bridge._token = "t" * 40
    store.set_sticky(storage, 2, 70, "another server's sticky", 9)

    async def run():
        async with TestClient(TestServer(bridge.build_app())) as client:
            async def act(actor, **body):
                response = await client.post("/v1/guilds/1/stickies", headers={"Authorization": "Bearer " + "t" * 40}, json={"user_id": str(actor), **body})
                return response.status, await response.json()

            assert (await act(5, action="set", channel_id="7", content="hi"))[0] == 403
            assert (await act(4, action="set", channel_id="8", content="hi"))[0] == 409      # the bot can't post there
            assert (await act(4, action="set", channel_id="7", content=""))[0] == 400
            status, done = await act(4, action="set", channel_id="7", content="read the rules", slow=True)
            assert status == 200 and done["detail"] == "Stuck the message in #chan7" and posted == [(7, "read the rules", 1)]
            assert store.get(storage, 7)["slow"] == 1
            assert (await act(4, action="stop", channel_id="7"))[1]["detail"] == "Stopped the sticky in #chan7" and store.get(storage, 7)["active"] == 0
            assert (await act(4, action="start", channel_id="7"))[0] == 200 and len(posted) == 2
            assert (await act(4, action="remove", channel_id="70"))[0] == 404                 # belongs to another server
            store.set_last_message(storage, 7, 555)
            assert (await act(4, action="remove", channel_id="7"))[0] == 200 and deleted == [555] and store.get(storage, 7) is None
            assert (await act(4, action="explode", channel_id="7"))[0] == 404

    asyncio.run(run())
    assert store.get(storage, 70) is not None
