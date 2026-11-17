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


def test_actions_check_their_options_and_only_messages_need_text(storage):
    soon = NOW + timedelta(hours=1)
    for action, options in (("slowmode", {}), ("slowmode", {"seconds": 99999}), ("purge", {"count": 0}), ("purge", {"count": 501}),
                            ("role_add", {"role_id": 5}), ("explode", {})):
        with pytest.raises(store.ScheduleError):
            store.add(storage, 1, 7, "", soon, 0, 9, action=action, options=options, now=NOW)
    lock = store.add(storage, 1, 7, "", soon, 1440, 9, action="lock", now=NOW)
    slow = store.add(storage, 1, 7, "Slow down", soon, 0, 9, action="slowmode", options={"seconds": "30", "junk": 1}, now=NOW)
    role = store.add(storage, 1, 7, "ignored", soon, 0, 9, action="role_add", options={"role_id": 5, "user_id": 6}, now=NOW)
    assert lock["action"] == "lock" and lock["content"] == "" and slow["options"] == {"seconds": 30}
    assert role["channel_id"] == 0 and role["content"] == "" and role["options"] == {"role_id": 5, "user_id": 6}
    assert [e["action"] for e in store.list_for_guild(storage, 1)] == ["lock", "slowmode", "role_add"]


def test_due_actions_are_carried_out(storage):
    done = []

    class Overwrite:
        send_messages = "unset"

    class Channel:
        id = 7

        def overwrites_for(self, role):
            return Overwrite()

        async def send(self, content, allowed_mentions=None):
            done.append(("send", content))

        async def set_permissions(self, role, overwrite=None, reason=None):
            done.append(("permissions", role, overwrite.send_messages))

        async def edit(self, slowmode_delay=None, reason=None):
            done.append(("slowmode", slowmode_delay))

        async def purge(self, limit=None, check=None, reason=None):
            done.append(("purge", limit, check(SimpleNamespace(pinned=True)), check(SimpleNamespace(pinned=False))))

    class Role:
        managed = False

        def __init__(self, position):
            self.position = position

        def is_default(self):
            return False

        def __ge__(self, other):
            return self.position >= other.position

    class Member:
        async def add_roles(self, role, reason=None):
            done.append(("give", role.position))

        async def remove_roles(self, role, reason=None):
            done.append(("take", role.position))

    roles = {5: Role(1), 6: Role(9)}
    guild = SimpleNamespace(id=1, default_role="@everyone", me=SimpleNamespace(top_role=Role(5)), get_channel={7: Channel()}.get,
                            get_member={3: Member()}.get, get_role=roles.get)
    cog = Schedules(SimpleNamespace(storage_path=storage, get_guild=lambda gid: guild if gid == 1 else None))
    soon = NOW + timedelta(minutes=1)
    store.add(storage, 1, 7, "Closed for the night", soon, 0, 9, action="lock", now=NOW)
    store.add(storage, 1, 7, "", soon, 0, 9, action="unlock", now=NOW)
    store.add(storage, 1, 7, "", soon, 0, 9, action="slowmode", options={"seconds": 30}, now=NOW)
    store.add(storage, 1, 7, "All clean", soon, 0, 9, action="purge", options={"count": 50}, now=NOW)
    store.add(storage, 1, 0, "", soon, 0, 9, action="role_add", options={"role_id": 5, "user_id": 3}, now=NOW)
    store.add(storage, 1, 0, "", soon, 0, 9, action="role_remove", options={"role_id": 5, "user_id": 3}, now=NOW)
    store.add(storage, 1, 0, "", soon, 0, 9, action="role_add", options={"role_id": 6, "user_id": 3}, now=NOW)  # above the bot
    store.add(storage, 1, 0, "", soon, 0, 9, action="role_add", options={"role_id": 5, "user_id": 4}, now=NOW)  # member left
    asyncio.run(cog.run_due(NOW + timedelta(minutes=2)))
    assert done == [
        ("send", "Closed for the night"), ("permissions", "@everyone", False), ("permissions", "@everyone", None),
        ("slowmode", 30), ("purge", 50, False, True), ("send", "All clean"), ("give", 1), ("take", 1),
    ]
    assert store.list_for_guild(storage, 1) == []


def test_a_module_that_is_off_keeps_its_schedule_for_later(storage):
    from utils.settings import update_guild_settings

    posted = []

    class Channel:
        id = 7

        async def send(self, content, allowed_mentions=None):
            posted.append(content)

    guild = SimpleNamespace(id=1, get_channel={7: Channel()}.get)
    cog = Schedules(SimpleNamespace(storage_path=storage, get_guild=lambda gid: guild))
    store.add(storage, 1, 7, "later", NOW + timedelta(minutes=1), 0, 9, now=NOW)
    update_guild_settings(storage, 1, {"schedules_enabled": False})
    asyncio.run(cog.run_due(NOW + timedelta(minutes=2)))
    assert posted == [] and len(store.list_for_guild(storage, 1)) == 1
    update_guild_settings(storage, 1, {"schedules_enabled": True})
    asyncio.run(cog.run_due(NOW + timedelta(minutes=3)))
    assert posted == ["later"]
