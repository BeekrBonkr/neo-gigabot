from __future__ import annotations

import asyncio
from types import SimpleNamespace

import discord

from utils import activity_log as store
from utils.db import connect
from utils.settings import update_guild_settings


def test_events_are_searchable_by_any_word_and_scoped_to_the_guild(storage):
    store.record(storage, 1, title="Message Deleted", description="in #general", fields=[("Content", "Hello 100% world_x")], user_id=5, channel_id=7)
    store.record(storage, 1, title="Member Joined", description="alice joined", user_id=6)
    store.record(storage, 2, title="Message Deleted", description="elsewhere")
    assert [r["title"] for r in store.search(storage, 1)] == ["Member Joined", "Message Deleted"]
    assert [r["title"] for r in store.search(storage, 1, query="DELETED hello")] == ["Message Deleted"]
    assert store.search(storage, 1, query="deleted alice") == []            # every word has to match
    assert len(store.search(storage, 1, query="100%")) == 1 and store.search(storage, 1, query="%") != store.search(storage, 1)
    assert len(store.search(storage, 1, query="world_x")) == 1 and store.search(storage, 1, query="worldAx") == []  # _ is not a wildcard
    row = store.search(storage, 1, query="hello")[0]
    assert row["body"]["fields"] == [["Content", "Hello 100% world_x"]] and row["user_id"] == 5 and row["channel_id"] == 7
    assert len(store.search(storage, 1, limit=1)) == 1


def test_keep_days_zero_stores_nothing_and_pruning_drops_old_and_excess_rows(storage, monkeypatch):
    store.record(storage, 1, title="Ignored", keep_days=0)
    assert store.search(storage, 1) == []
    for n in range(5):
        store.record(storage, 1, title=f"e{n}")
    with connect(storage) as connection:
        connection.execute("UPDATE activity_log SET created_at = datetime('now', '-40 days') WHERE title IN ('e0', 'e1')")
    store.prune(storage, 1, 30)
    assert [r["title"] for r in store.search(storage, 1)] == ["e4", "e3", "e2"]
    monkeypatch.setattr(store, "MAX_ROWS_PER_GUILD", 2)
    store.prune(storage, 1, 30)
    assert [r["title"] for r in store.search(storage, 1)] == ["e4", "e3"]
    store.prune(storage, 1, 0)
    assert store.search(storage, 1) == []


def test_the_logging_cog_keeps_a_copy_only_while_logging_is_on(storage):
    from cogs.logging import Logging

    cog = Logging(SimpleNamespace(storage_path=storage))
    guild = SimpleNamespace(id=1, get_channel=lambda cid: None)
    embed = discord.Embed(title="Role Created", description="@mods")
    embed.add_field(name="By", value="admin")
    asyncio.run(cog._log(guild, embed, user_id=5))
    assert store.search(storage, 1) == []                                   # logging is off
    update_guild_settings(storage, 1, {"logging_enabled": True})
    asyncio.run(cog._log(guild, embed, user_id=5))
    assert [r["title"] for r in store.search(storage, 1, query="mods admin")] == ["Role Created"]
    update_guild_settings(storage, 1, {"ignored_users": [5]})
    asyncio.run(cog._log(guild, embed, user_id=5))
    assert len(store.search(storage, 1)) == 1                               # ignored users stay out of the copy too
    update_guild_settings(storage, 1, {"ignored_users": [], "logging_keep_days": 0})
    asyncio.run(cog._log(guild, embed, user_id=5))
    assert len(store.search(storage, 1)) == 1


def test_every_servers_retention_is_applied_even_when_that_server_is_quiet(storage):
    for guild_id in (1, 2, 3):
        store.record(storage, guild_id, title="Message Deleted", description="old")
        store.record(storage, guild_id, title="Message Deleted", description="new")
    with connect(storage) as connection:
        connection.execute("UPDATE activity_log SET created_at = datetime('now', '-200 days') WHERE search LIKE '%old%'")
    removed = store.prune_all(storage, {1: 7, 2: 0, 3: 365}.get)
    assert removed == 3
    assert [row["body"]["description"] for row in store.search(storage, 1)] == ["new"]  # past its 7 days
    assert store.search(storage, 2) == []                                               # keeps no copy
    assert len(store.search(storage, 3)) == 2                                           # a year is still inside
