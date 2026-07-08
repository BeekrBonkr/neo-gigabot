from __future__ import annotations

from pathlib import Path
from typing import Any

from utils.db import connect, write_transaction

CREATE_STARBOARD_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS starboard_posts (
    guild_id INTEGER NOT NULL,
    message_id INTEGER NOT NULL,
    starboard_message_id INTEGER NOT NULL,
    star_count INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (guild_id, message_id)
)
"""


def ensure_starboard_table(storage_path: Path) -> None:
    with connect(storage_path) as connection:
        connection.execute(CREATE_STARBOARD_TABLE_SQL)
        connection.commit()


def get_starboard_post(storage_path: Path, guild_id: int, message_id: int) -> dict[str, Any] | None:
    ensure_starboard_table(storage_path)
    with connect(storage_path) as connection:
        row = connection.execute(
            "SELECT * FROM starboard_posts WHERE guild_id = ? AND message_id = ?",
            (int(guild_id), int(message_id)),
        ).fetchone()
    return dict(row) if row else None


def save_starboard_post(
    storage_path: Path,
    guild_id: int,
    message_id: int,
    starboard_message_id: int,
    star_count: int,
) -> None:
    ensure_starboard_table(storage_path)
    with write_transaction(storage_path) as connection:
        connection.execute(
            """
            INSERT INTO starboard_posts (guild_id, message_id, starboard_message_id, star_count)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(guild_id, message_id) DO UPDATE SET
                starboard_message_id = excluded.starboard_message_id,
                star_count = excluded.star_count
            """,
            (int(guild_id), int(message_id), int(starboard_message_id), int(star_count)),
        )


def delete_starboard_post(storage_path: Path, guild_id: int, message_id: int) -> bool:
    ensure_starboard_table(storage_path)
    with write_transaction(storage_path) as connection:
        cursor = connection.execute(
            "DELETE FROM starboard_posts WHERE guild_id = ? AND message_id = ?",
            (int(guild_id), int(message_id)),
        )
        return cursor.rowcount > 0
