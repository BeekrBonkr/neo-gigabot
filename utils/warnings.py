from __future__ import annotations

from pathlib import Path
from typing import Any

from utils.db import connect, write_transaction

CREATE_WARNINGS_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS warnings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    moderator_id INTEGER NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
)
"""


def ensure_warnings_table(storage_path: Path) -> None:
    with connect(storage_path) as connection:
        connection.execute(CREATE_WARNINGS_TABLE_SQL)
        connection.commit()


def add_warning(storage_path: Path, guild_id: int, user_id: int, moderator_id: int, reason: str) -> int:
    ensure_warnings_table(storage_path)
    with write_transaction(storage_path) as connection:
        cursor = connection.execute(
            "INSERT INTO warnings (guild_id, user_id, moderator_id, reason) VALUES (?, ?, ?, ?)",
            (int(guild_id), int(user_id), int(moderator_id), reason or ""),
        )
        return cursor.lastrowid


def list_warnings(storage_path: Path, guild_id: int, user_id: int) -> list[dict[str, Any]]:
    ensure_warnings_table(storage_path)
    with connect(storage_path) as connection:
        rows = connection.execute(
            "SELECT * FROM warnings WHERE guild_id = ? AND user_id = ? ORDER BY created_at DESC",
            (int(guild_id), int(user_id)),
        ).fetchall()
    return [dict(row) for row in rows]


def count_warnings(storage_path: Path, guild_id: int, user_id: int, *, within_days: int | None = None) -> int:
    """Count a member's warnings, optionally only those issued in the last
    `within_days` days (created_at is stored by SQLite as UTC)."""
    ensure_warnings_table(storage_path)
    query = "SELECT COUNT(*) AS c FROM warnings WHERE guild_id = ? AND user_id = ?"
    params: list[Any] = [int(guild_id), int(user_id)]
    if within_days is not None and within_days > 0:
        query += " AND created_at >= datetime('now', ?)"
        params.append(f"-{int(within_days)} days")
    with connect(storage_path) as connection:
        row = connection.execute(query, params).fetchone()
    return row["c"] if row else 0


def remove_warning(storage_path: Path, guild_id: int, warning_id: int) -> dict[str, Any] | None:
    """Delete one warning by its ID. Returns the removed row, or None if no
    warning with that ID exists in this guild (the guild check stops one
    server's mods from deleting another server's warnings by guessing IDs)."""
    ensure_warnings_table(storage_path)
    with write_transaction(storage_path) as connection:
        row = connection.execute(
            "SELECT * FROM warnings WHERE id = ? AND guild_id = ?",
            (int(warning_id), int(guild_id)),
        ).fetchone()
        if row is None:
            return None
        connection.execute("DELETE FROM warnings WHERE id = ?", (int(warning_id),))
        return dict(row)


def clear_warnings(storage_path: Path, guild_id: int, user_id: int) -> int:
    """Delete all of a member's warnings in a guild. Returns how many were removed."""
    ensure_warnings_table(storage_path)
    with write_transaction(storage_path) as connection:
        cursor = connection.execute(
            "DELETE FROM warnings WHERE guild_id = ? AND user_id = ?",
            (int(guild_id), int(user_id)),
        )
        return cursor.rowcount
