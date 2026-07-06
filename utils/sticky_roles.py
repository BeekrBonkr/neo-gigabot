from __future__ import annotations

import json
from pathlib import Path

from utils.db import connect, write_transaction

CREATE_STICKY_ROLES_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS sticky_roles (
    guild_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    role_ids TEXT NOT NULL,
    PRIMARY KEY (guild_id, user_id)
)
"""


def ensure_sticky_roles_table(storage_path: Path) -> None:
    with connect(storage_path) as connection:
        connection.execute(CREATE_STICKY_ROLES_TABLE_SQL)
        connection.commit()


def save_roles(storage_path: Path, guild_id: int, user_id: int, role_ids: list[int]) -> None:
    """Remember a departing member's roles so they can be restored if they rejoin."""
    ensure_sticky_roles_table(storage_path)
    with write_transaction(storage_path) as connection:
        if not role_ids:
            connection.execute(
                "DELETE FROM sticky_roles WHERE guild_id = ? AND user_id = ?",
                (int(guild_id), int(user_id)),
            )
            return
        connection.execute(
            """
            INSERT INTO sticky_roles (guild_id, user_id, role_ids)
            VALUES (?, ?, ?)
            ON CONFLICT(guild_id, user_id) DO UPDATE SET role_ids = excluded.role_ids
            """,
            (int(guild_id), int(user_id), json.dumps([int(r) for r in role_ids])),
        )


def pop_roles(storage_path: Path, guild_id: int, user_id: int) -> list[int]:
    """Fetch and delete the stored role IDs for a rejoining member, in one transaction."""
    ensure_sticky_roles_table(storage_path)
    with write_transaction(storage_path) as connection:
        row = connection.execute(
            "SELECT role_ids FROM sticky_roles WHERE guild_id = ? AND user_id = ?",
            (int(guild_id), int(user_id)),
        ).fetchone()
        if row is None:
            return []
        connection.execute(
            "DELETE FROM sticky_roles WHERE guild_id = ? AND user_id = ?",
            (int(guild_id), int(user_id)),
        )
        try:
            loaded = json.loads(row["role_ids"])
        except (TypeError, ValueError):
            return []
        if not isinstance(loaded, list):
            return []
        return [int(r) for r in loaded]
