from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from cogs.antiraid import AntiRaid, JoinTracker, account_too_new
from utils.cases import list_cases
from utils.settings import update_guild_settings


def test_a_burst_of_joins_trips_the_limit_and_a_trickle_does_not():
    tracker = JoinTracker()
    assert [tracker.record_join(1, 3, 10, now=t) for t in (0, 1, 2)] == [False, False, True]
    slow = JoinTracker()
    assert [slow.record_join(1, 3, 10, now=t) for t in (0, 11, 22, 33)] == [False] * 4
    assert slow.record_join(2, 3, 10, now=33) is False  # servers are counted separately


def test_lockdown_runs_out_and_can_be_ended_early():
    tracker = JoinTracker()
    assert tracker.remaining(1, now=0) == 0
    tracker.lock(1, 10, now=0)
    assert tracker.remaining(1, now=60) == 540 and tracker.remaining(1, now=601) == 0
    tracker.lock(1, 10, now=0)
    assert tracker.unlock(1) is True and tracker.unlock(1) is False and tracker.remaining(1, now=1) == 0


def test_account_age_check():
    now = datetime(2026, 1, 10, tzinfo=timezone.utc)
    assert account_too_new(now - timedelta(days=2), 7, now) is True
    assert account_too_new(now - timedelta(days=8), 7, now) is False
    assert account_too_new(now, 0, now) is False  # 0 turns the check off


def _world(storage):
    alerts = []

    async def send(embed=None, **_):
        alerts.append(embed)

    channel = SimpleNamespace(send=send)
    guild = SimpleNamespace(id=1, name="G", get_channel=lambda cid: channel if cid == 99 else None)
    embeds = SimpleNamespace(warning_embed=lambda title, text: title)
    cog = AntiRaid(SimpleNamespace(storage_path=storage, user=SimpleNamespace(id=42), embeds=embeds))
    update_guild_settings(storage, 1, {"mod_log_channel_id": "99"})

    def member(uid, age_days=400):
        kicked = []

        async def kick(reason=None):
            kicked.append(reason)

        async def dm(embed=None):
            pass

        return SimpleNamespace(id=uid, bot=False, guild=guild, kick=kick, send=dm, kicked=kicked,
                               created_at=datetime.now(timezone.utc) - timedelta(days=age_days))

    return cog, member, alerts


def test_young_accounts_are_kicked_and_recorded_even_with_raid_detection_off(storage):
    cog, member, _ = _world(storage)
    update_guild_settings(storage, 1, {"antiraid_min_account_age_days": 7})
    young, old = member(5, age_days=1), member(6)
    asyncio.run(cog.on_member_join(young))
    asyncio.run(cog.on_member_join(old))
    assert young.kicked == ["Account younger than 7 day(s)"] and old.kicked == []
    assert [c["action"] for c in list_cases(storage, 1, 5)] == ["Anti-Raid Kick"] and list_cases(storage, 1, 6) == []


def test_a_raid_starts_a_lockdown_that_only_affects_later_joiners(storage):
    cog, member, alerts = _world(storage)
    update_guild_settings(storage, 1, {"antiraid_enabled": True, "antiraid_join_limit": 3, "antiraid_window_seconds": 30})
    first = [member(n) for n in (1, 2, 3)]
    for joiner in first:
        asyncio.run(cog.on_member_join(joiner))
    assert all(j.kicked == [] for j in first) and alerts == ["🛡️ Lockdown Started"]
    late = member(4)
    asyncio.run(cog.on_member_join(late))
    assert late.kicked == ["Joined during a lockdown"]
    cog.tracker.unlock(1)
    after = member(5)
    asyncio.run(cog.on_member_join(after))
    assert after.kicked == []


def test_nothing_happens_while_everything_is_off(storage):
    cog, member, alerts = _world(storage)
    joiners = [member(n, age_days=0) for n in range(20)]
    for joiner in joiners:
        asyncio.run(cog.on_member_join(joiner))
    assert all(j.kicked == [] for j in joiners) and alerts == []
