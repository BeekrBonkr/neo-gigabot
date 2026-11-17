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
