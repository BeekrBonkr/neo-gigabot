"""What the honeypot remembers per server: where its notice message is, and
how many accounts it has caught."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from utils.db import connect

CREATE_SQL = """
CREATE TABLE IF NOT EXISTS honeypot_state (
    guild_id INTEGER PRIMARY KEY,
    channel_id INTEGER NOT NULL DEFAULT 0,
    message_id INTEGER NOT NULL DEFAULT 0,
    caught INTEGER NOT NULL DEFAULT 0
)
"""


def ensure_table(storage_path: Path) -> None:
    with connect(storage_path) as connection:
        connection.execute(CREATE_SQL)


def get_state(storage_path: Path, guild_id: int) -> dict[str, Any]:
    ensure_table(storage_path)
    with connect(storage_path) as connection:
        row = connection.execute("SELECT * FROM honeypot_state WHERE guild_id = ?", (int(guild_id),)).fetchone()
    return dict(row) if row else {"guild_id": int(guild_id), "channel_id": 0, "message_id": 0, "caught": 0}


def set_notice(storage_path: Path, guild_id: int, channel_id: int, message_id: int) -> None:
    """Remember which message is the notice, keeping the count."""
    ensure_table(storage_path)
    with connect(storage_path) as connection:
        connection.execute(
            "INSERT INTO honeypot_state (guild_id, channel_id, message_id) VALUES (?, ?, ?) "
            "ON CONFLICT(guild_id) DO UPDATE SET channel_id = excluded.channel_id, message_id = excluded.message_id",
            (int(guild_id), int(channel_id), int(message_id)),
        )


def record_catch(storage_path: Path, guild_id: int) -> int:
    """Count one more caught account. Returns the new total."""
    ensure_table(storage_path)
    with connect(storage_path) as connection:
        connection.execute(
            "INSERT INTO honeypot_state (guild_id, caught) VALUES (?, 1) ON CONFLICT(guild_id) DO UPDATE SET caught = caught + 1",
            (int(guild_id),),
        )
        return int(connection.execute("SELECT caught FROM honeypot_state WHERE guild_id = ?", (int(guild_id),)).fetchone()[0])
