from __future__ import annotations

from pathlib import Path
from typing import Any

from utils.db import connect, write_transaction

MAX_MAPPINGS_PER_GUILD = 100

CREATE_REACTION_ROLES_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS reaction_roles (
    guild_id INTEGER NOT NULL,
    message_id INTEGER NOT NULL,
    channel_id INTEGER NOT NULL,
    emoji TEXT NOT NULL,
    role_id INTEGER NOT NULL,
    PRIMARY KEY (message_id, emoji)
)
"""


class ReactionRoleError(ValueError):
    """User-facing reaction-role error. Message is safe to show as-is."""


def ensure_reaction_roles_table(storage_path: Path) -> None:
    with connect(storage_path) as connection:
        connection.execute(CREATE_REACTION_ROLES_TABLE_SQL)
        connection.commit()


def _row_to_dict(row) -> dict[str, Any]:
    return {
        "guild_id": int(row["guild_id"]),
        "message_id": int(row["message_id"]),
        "channel_id": int(row["channel_id"]),
        "emoji": row["emoji"],
        "role_id": int(row["role_id"]),
    }


def count_for_guild(storage_path: Path, guild_id: int) -> int:
    ensure_reaction_roles_table(storage_path)
    with connect(storage_path) as connection:
        row = connection.execute(
            "SELECT COUNT(*) AS c FROM reaction_roles WHERE guild_id = ?", (int(guild_id),)
        ).fetchone()
    return row["c"] if row else 0


def add_mapping(
    storage_path: Path,
    guild_id: int,
    message_id: int,
    channel_id: int,
    emoji: str,
    role_id: int,
) -> None:
    """Add (or replace) the role given for a specific emoji on a specific message."""
    ensure_reaction_roles_table(storage_path)

    with write_transaction(storage_path) as connection:
        existing = connection.execute(
            "SELECT 1 FROM reaction_roles WHERE message_id = ? AND emoji = ?",
            (int(message_id), emoji),
        ).fetchone()

        if existing is None:
            count = connection.execute(
                "SELECT COUNT(*) AS c FROM reaction_roles WHERE guild_id = ?", (int(guild_id),)
            ).fetchone()["c"]
            if count >= MAX_MAPPINGS_PER_GUILD:
                raise ReactionRoleError(
                    f"This server already has the maximum of {MAX_MAPPINGS_PER_GUILD} reaction-role mappings."
                )

        connection.execute(
            """
            INSERT INTO reaction_roles (guild_id, message_id, channel_id, emoji, role_id)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(message_id, emoji) DO UPDATE SET role_id = excluded.role_id
            """,
            (int(guild_id), int(message_id), int(channel_id), emoji, int(role_id)),
        )


def import_reaction_role_raw(
    storage_path: Path,
    guild_id: int,
    message_id: int,
    channel_id: int,
    emoji: str,
    role_id: int,
) -> None:
    """Like add_mapping, but bypasses MAX_MAPPINGS_PER_GUILD - used by
    utils/migration.py. A migration shouldn't silently drop real historical
    mappings to enforce a cap that exists to stop *future* abuse via the live
    /reactionrole commands."""
    ensure_reaction_roles_table(storage_path)
    with write_transaction(storage_path) as connection:
        connection.execute(
            """
            INSERT INTO reaction_roles (guild_id, message_id, channel_id, emoji, role_id)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(message_id, emoji) DO UPDATE SET role_id = excluded.role_id
            """,
            (int(guild_id), int(message_id), int(channel_id), emoji, int(role_id)),
        )


def remove_mapping(storage_path: Path, message_id: int, emoji: str) -> bool:
    ensure_reaction_roles_table(storage_path)
    with write_transaction(storage_path) as connection:
        cursor = connection.execute(
            "DELETE FROM reaction_roles WHERE message_id = ? AND emoji = ?",
            (int(message_id), emoji),
        )
        return cursor.rowcount > 0


def clear_message(storage_path: Path, message_id: int) -> list[dict[str, Any]]:
    """Remove every mapping for a message. Returns the removed rows (so the
    caller can also strip the bot's own reactions for each emoji)."""
    ensure_reaction_roles_table(storage_path)
    with write_transaction(storage_path) as connection:
        rows = connection.execute(
            "SELECT * FROM reaction_roles WHERE message_id = ?", (int(message_id),)
        ).fetchall()
        connection.execute("DELETE FROM reaction_roles WHERE message_id = ?", (int(message_id),))
        return [_row_to_dict(row) for row in rows]


def get_role_for_reaction(storage_path: Path, message_id: int, emoji: str) -> int | None:
    """Fast path used by the raw reaction listeners, which fire on every
    reaction bot-wide - keep this cheap."""
    ensure_reaction_roles_table(storage_path)
    with connect(storage_path) as connection:
        row = connection.execute(
            "SELECT role_id FROM reaction_roles WHERE message_id = ? AND emoji = ?",
            (int(message_id), emoji),
        ).fetchone()
    return int(row["role_id"]) if row else None


def list_for_message(storage_path: Path, message_id: int) -> list[dict[str, Any]]:
    ensure_reaction_roles_table(storage_path)
    with connect(storage_path) as connection:
        rows = connection.execute(
            "SELECT * FROM reaction_roles WHERE message_id = ? ORDER BY emoji", (int(message_id),)
        ).fetchall()
    return [_row_to_dict(row) for row in rows]


def list_for_guild(storage_path: Path, guild_id: int) -> list[dict[str, Any]]:
    ensure_reaction_roles_table(storage_path)
    with connect(storage_path) as connection:
        rows = connection.execute(
            "SELECT * FROM reaction_roles WHERE guild_id = ? ORDER BY message_id, emoji", (int(guild_id),)
        ).fetchall()
    return [_row_to_dict(row) for row in rows]
