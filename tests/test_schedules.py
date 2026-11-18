from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import discord
import pytest

from cogs.schedules import Schedules, describe_interval
from utils import schedules as store

NOW = datetime(2026, 3, 1, 12, 0, tzinfo=timezone.utc)


def test_times_and_text_are_validated_and_capped_per_server(storage):
    for content, run_at, interval in (("", NOW + timedelta(hours=1), 0), ("x" * 2001, NOW + timedelta(hours=1), 0),
                                      ("hi", NOW - timedelta(hours=1), 0), ("hi", NOW + timedelta(days=400), 0), ("hi", NOW + timedelta(hours=1), 5)):
        with pytest.raises(store.ScheduleError):
            store.add(storage, 1, 7, content, run_at, interval, 9, now=NOW)
    for n in range(store.MAX_SCHEDULES_PER_GUILD):
        store.add(storage, 1, 7, f"m{n}", NOW + timedelta(hours=1), 0, 9, now=NOW)
    with pytest.raises(store.ScheduleError, match="already has"):
        store.add(storage, 1, 7, "one more", NOW + timedelta(hours=1), 0, 9, now=NOW)
    assert store.add(storage, 2, 7, "other server", NOW + timedelta(hours=1), 0, 9, now=NOW)["guild_id"] == 2


def test_timestamps_are_read_as_utc():
    assert store.parse_utc("2026-03-01T12:00:00Z") == NOW and store.parse_utc("2026-03-01 12:00:00") == NOW
    assert store.parse_utc("2026-03-01T14:00:00+02:00") == NOW and store.parse_utc("tomorrow") is None


def test_one_offs_finish_and_repeats_skip_missed_runs(storage):
    once = store.add(storage, 1, 7, "once", NOW + timedelta(minutes=5), 0, 9, now=NOW)
    daily = store.add(storage, 1, 7, "daily", NOW + timedelta(minutes=5), 1440, 9, now=NOW)
    assert store.due(storage, NOW) == [] and len(store.due(storage, NOW + timedelta(minutes=6))) == 2
    assert store.finish_run(storage, once, NOW + timedelta(minutes=6)) is None
    # The bot was down for three days: the daily message runs once and lands on its usual time tomorrow.
    late = NOW + timedelta(days=3, minutes=30)
    assert store.finish_run(storage, daily, late) == NOW + timedelta(days=4, minutes=5)
    remaining = store.list_for_guild(storage, 1)
    assert [e["content"] for e in remaining] == ["daily"] and store.due(storage, late) == []
    assert store.remove(storage, 2, daily["id"]) is None and store.remove(storage, 1, daily["id"])["content"] == "daily"


def test_intervals_read_naturally():
    assert [describe_interval(m) for m in (0, 60, 120, 1440, 10080, 20160, 45)] == [
        "once", "every hour", "every 2 hours", "every day", "every week", "every 2 weeks", "every 45 minutes"]


def test_due_messages_are_posted_and_dead_channels_are_dropped(storage):
    posted = []

    class Channel:
        def __init__(self, fail=None):
            self.id, self.fail = 7, fail

        async def send(self, content, allowed_mentions=None):
            if self.fail:
                raise self.fail
            posted.append(content)

    channels = {7: Channel()}
    guild = SimpleNamespace(id=1, get_channel=channels.get)
    cog = Schedules(SimpleNamespace(storage_path=storage, get_guild=lambda gid: guild if gid == 1 else None))
    store.add(storage, 1, 7, "hello @everyone", NOW + timedelta(minutes=1), 0, 9, now=NOW)
    store.add(storage, 1, 7, "weekly", NOW + timedelta(minutes=1), 10080, 9, now=NOW)
    store.add(storage, 1, 8, "channel was deleted", NOW + timedelta(minutes=1), 0, 9, now=NOW)
    store.add(storage, 5, 7, "bot left that server", NOW + timedelta(minutes=1), 0, 9, now=NOW)
    asyncio.run(cog.run_due(NOW))
    assert posted == []
    asyncio.run(cog.run_due(NOW + timedelta(minutes=2)))
    assert sorted(posted) == ["hello @everyone", "weekly"]
    assert [e["content"] for e in store.list_for_guild(storage, 1)] == ["weekly"] and store.list_for_guild(storage, 5) == []
    # A transient failure keeps the message for the next tick.
    channels[7] = Channel(discord.HTTPException(SimpleNamespace(status=500, reason="x"), "boom"))
    asyncio.run(cog.run_due(NOW + timedelta(days=8)))
    assert len(store.due(storage, NOW + timedelta(days=8))) == 1
