from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from utils.tempbans import add_tempban, due_tempbans, get_tempban, remove_tempban

GUILD = 1
USER = 2
MOD = 3


def test_add_and_get(storage: Path) -> None:
    unban_at = datetime.now(timezone.utc) + timedelta(days=1)
    add_tempban(storage, GUILD, USER, MOD, "cooldown", unban_at)

    entry = get_tempban(storage, GUILD, USER)
    assert entry is not None
    assert entry["reason"] == "cooldown"
    assert entry["moderator_id"] == MOD


def test_re_tempban_updates_schedule(storage: Path) -> None:
    add_tempban(storage, GUILD, USER, MOD, "first", datetime.now(timezone.utc) + timedelta(days=1))
    add_tempban(storage, GUILD, USER, MOD + 1, "second", datetime.now(timezone.utc) + timedelta(days=2))

    entry = get_tempban(storage, GUILD, USER)
    assert entry["reason"] == "second"
    assert entry["moderator_id"] == MOD + 1


def test_due_tempbans_only_returns_expired(storage: Path) -> None:
    now = datetime.now(timezone.utc)
    add_tempban(storage, GUILD, USER, MOD, "expired", now - timedelta(minutes=5))
    add_tempban(storage, GUILD, USER + 1, MOD, "future", now + timedelta(days=1))

    due = due_tempbans(storage, now=now)
    assert [entry["user_id"] for entry in due] == [USER]


def test_remove_tempban(storage: Path) -> None:
    add_tempban(storage, GUILD, USER, MOD, "x", datetime.now(timezone.utc))
    assert remove_tempban(storage, GUILD, USER) is True
    assert remove_tempban(storage, GUILD, USER) is False
    assert get_tempban(storage, GUILD, USER) is None


def test_one_failing_tempban_does_not_stop_the_others_or_the_loop(storage: Path, monkeypatch) -> None:
    import asyncio
    import sqlite3
    from types import SimpleNamespace

    import cogs.moderation as moderation
    from modfakes import make_bot

    past = datetime.now(timezone.utc) - timedelta(minutes=5)
    add_tempban(storage, GUILD, USER, MOD, "first", past)
    add_tempban(storage, GUILD, USER + 1, MOD, "second", past)
    unbanned = []

    async def unban(target, reason=None):
        if target.id == USER:
            raise RuntimeError("something nobody expected")
        unbanned.append(target.id)

    bot = make_bot(storage)
    bot.get_guild = lambda guild_id: SimpleNamespace(id=guild_id, unban=unban)
    cog = moderation.Moderation(bot)

    async def nobody(user_id):
        return None

    cog._fetch_user_quiet = nobody
    asyncio.run(cog._process_due_tempbans.coro(cog))  # must not raise
    assert unbanned == [USER + 1]
    assert get_tempban(storage, GUILD, USER) is not None and get_tempban(storage, GUILD, USER + 1) is None

    def locked(path):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(moderation, "due_tempbans", locked)
    asyncio.run(cog._process_due_tempbans.coro(cog))  # nor this


def test_a_failed_begin_reports_the_real_error(storage: Path) -> None:
    import sqlite3

    import pytest

    from utils import db

    with db.write_transaction(storage) as connection:
        connection.execute("CREATE TABLE IF NOT EXISTS t (x)")
    holder = db.connect(storage)
    holder.isolation_level = None
    holder.execute("BEGIN IMMEDIATE")
    original = db.connect

    def impatient(path):
        connection = original(path)
        connection.execute("PRAGMA busy_timeout = 50")
        return connection

    db.connect = impatient
    try:
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            with db.write_transaction(storage):
                pass
    finally:
        db.connect = original
        holder.execute("ROLLBACK")
        holder.close()
