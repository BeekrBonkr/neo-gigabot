from __future__ import annotations

from pathlib import Path

from utils.db import write_transaction
from utils.warnings import (
    add_warning,
    clear_warnings,
    count_warnings,
    ensure_warnings_table,
    list_warnings,
    remove_warning,
)

GUILD = 1
USER = 2
MOD = 3


def test_add_list_count_round_trip(storage: Path) -> None:
    first_id = add_warning(storage, GUILD, USER, MOD, "spam")
    add_warning(storage, GUILD, USER, MOD, "more spam")

    entries = list_warnings(storage, GUILD, USER)
    assert len(entries) == 2
    assert {e["reason"] for e in entries} == {"spam", "more spam"}
    assert count_warnings(storage, GUILD, USER) == 2
    assert isinstance(first_id, int)


def test_counts_are_scoped_per_guild_and_user(storage: Path) -> None:
    add_warning(storage, GUILD, USER, MOD, "here")
    add_warning(storage, GUILD + 1, USER, MOD, "elsewhere")
    add_warning(storage, GUILD, USER + 1, MOD, "someone else")
    assert count_warnings(storage, GUILD, USER) == 1


def test_remove_warning_by_id(storage: Path) -> None:
    warning_id = add_warning(storage, GUILD, USER, MOD, "oops")
    removed = remove_warning(storage, GUILD, warning_id)
    assert removed is not None
    assert removed["reason"] == "oops"
    assert count_warnings(storage, GUILD, USER) == 0
    assert remove_warning(storage, GUILD, warning_id) is None


def test_remove_warning_wrong_guild_refused(storage: Path) -> None:
    warning_id = add_warning(storage, GUILD, USER, MOD, "oops")
    assert remove_warning(storage, GUILD + 1, warning_id) is None
    assert count_warnings(storage, GUILD, USER) == 1


def test_clear_warnings(storage: Path) -> None:
    add_warning(storage, GUILD, USER, MOD, "a")
    add_warning(storage, GUILD, USER, MOD, "b")
    add_warning(storage, GUILD, USER + 1, MOD, "c")

    assert clear_warnings(storage, GUILD, USER) == 2
    assert count_warnings(storage, GUILD, USER) == 0
    assert count_warnings(storage, GUILD, USER + 1) == 1
    assert clear_warnings(storage, GUILD, USER) == 0


def test_count_warnings_window(storage: Path) -> None:
    add_warning(storage, GUILD, USER, MOD, "recent")
    ensure_warnings_table(storage)
    # Backdate a second warning past any reasonable window.
    with write_transaction(storage) as connection:
        connection.execute(
            "INSERT INTO warnings (guild_id, user_id, moderator_id, reason, created_at)"
            " VALUES (?, ?, ?, 'ancient', datetime('now', '-90 days'))",
            (GUILD, USER, MOD),
        )

    assert count_warnings(storage, GUILD, USER) == 2
    assert count_warnings(storage, GUILD, USER, within_days=30) == 1
    assert count_warnings(storage, GUILD, USER, within_days=0) == 2  # 0 = no window
