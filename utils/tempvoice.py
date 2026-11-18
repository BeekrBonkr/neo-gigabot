"""Which voice channels the bot created as temporary ones, so they can be
deleted when they empty out, including after a restart."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from utils.db import connect

CREATE_SQL = """
CREATE TABLE IF NOT EXISTS temp_voice_channels (
    channel_id INTEGER PRIMARY KEY,
    guild_id INTEGER NOT NULL,
    owner_id INTEGER NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
"""


def ensure_table(storage_path: Path) -> None:
    with connect(storage_path) as connection:
        connection.executescript(CREATE_SQL)


def add(storage_path: Path, channel_id: int, guild_id: int, owner_id: int) -> None:
    ensure_table(storage_path)
    with connect(storage_path) as connection:
        connection.execute("INSERT OR REPLACE INTO temp_voice_channels (channel_id, guild_id, owner_id) VALUES (?, ?, ?)",
                           (int(channel_id), int(guild_id), int(owner_id)))


def remove(storage_path: Path, channel_id: int) -> None:
    ensure_table(storage_path)
    with connect(storage_path) as connection:
        connection.execute("DELETE FROM temp_voice_channels WHERE channel_id = ?", (int(channel_id),))


def get(storage_path: Path, channel_id: int) -> dict[str, Any] | None:
    ensure_table(storage_path)
    with connect(storage_path) as connection:
        row = connection.execute("SELECT * FROM temp_voice_channels WHERE channel_id = ?", (int(channel_id),)).fetchone()
    return dict(row) if row else None


def for_owner(storage_path: Path, guild_id: int, owner_id: int) -> list[int]:
    ensure_table(storage_path)
    with connect(storage_path) as connection:
        rows = connection.execute("SELECT channel_id FROM temp_voice_channels WHERE guild_id = ? AND owner_id = ?", (int(guild_id), int(owner_id))).fetchall()
    return [int(row[0]) for row in rows]


def all_channels(storage_path: Path) -> list[dict[str, Any]]:
    ensure_table(storage_path)
    with connect(storage_path) as connection:
        return [dict(row) for row in connection.execute("SELECT * FROM temp_voice_channels").fetchall()]
