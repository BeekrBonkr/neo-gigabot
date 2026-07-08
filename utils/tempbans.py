from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from utils.db import connect, write_transaction

CREATE_TEMPBANS_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS tempbans (
    guild_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    moderator_id INTEGER NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    unban_at TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (guild_id, user_id)
)
"""


def _to_iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def ensure_tempbans_table(storage_path: Path) -> None:
    with connect(storage_path) as connection:
        connection.execute(CREATE_TEMPBANS_TABLE_SQL)
        connection.commit()


def add_tempban(
    storage_path: Path,
    guild_id: int,
    user_id: int,
    moderator_id: int,
    reason: str,
    unban_at: datetime,
) -> None:
    """Record (or update, if re-tempbanned) a scheduled unban."""
    ensure_tempbans_table(storage_path)
    with write_transaction(storage_path) as connection:
        connection.execute(
            """
            INSERT INTO tempbans (guild_id, user_id, moderator_id, reason, unban_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(guild_id, user_id) DO UPDATE SET
                moderator_id = excluded.moderator_id,
                reason = excluded.reason,
                unban_at = excluded.unban_at
            """,
            (int(guild_id), int(user_id), int(moderator_id), reason or "", _to_iso(unban_at)),
        )


def remove_tempban(storage_path: Path, guild_id: int, user_id: int) -> bool:
    """Drop a scheduled unban (used both after unbanning and when a mod
    manually unbans early via /unban). Returns whether a row existed."""
    ensure_tempbans_table(storage_path)
    with write_transaction(storage_path) as connection:
        cursor = connection.execute(
            "DELETE FROM tempbans WHERE guild_id = ? AND user_id = ?",
            (int(guild_id), int(user_id)),
        )
        return cursor.rowcount > 0


def due_tempbans(storage_path: Path, *, now: datetime | None = None) -> list[dict[str, Any]]:
    """All tempbans whose unban time has passed."""
    ensure_tempbans_table(storage_path)
    moment = now or datetime.now(timezone.utc)
    with connect(storage_path) as connection:
        rows = connection.execute(
            "SELECT * FROM tempbans WHERE unban_at <= ? ORDER BY unban_at",
            (_to_iso(moment),),
        ).fetchall()
    return [dict(row) for row in rows]


def get_tempban(storage_path: Path, guild_id: int, user_id: int) -> dict[str, Any] | None:
    ensure_tempbans_table(storage_path)
    with connect(storage_path) as connection:
        row = connection.execute(
            "SELECT * FROM tempbans WHERE guild_id = ? AND user_id = ?",
            (int(guild_id), int(user_id)),
        ).fetchone()
    return dict(row) if row else None
