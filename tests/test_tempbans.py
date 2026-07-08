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
