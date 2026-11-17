"""Moderation case history and private moderator notes.

A case is one moderation action (warn, mute, kick, ban, ...) against one user,
recorded whether or not the server has a mod-log channel. Notes are free text
moderators leave on a member for each other; members never see them.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from utils.db import connect

MAX_NOTE_LENGTH = 500
MAX_NOTES_PER_MEMBER = 50

CREATE_SQL = """
CREATE TABLE IF NOT EXISTS mod_cases (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    moderator_id INTEGER NOT NULL,
    action TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    duration TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS mod_cases_member ON mod_cases (guild_id, user_id);
CREATE TABLE IF NOT EXISTS mod_notes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    author_id INTEGER NOT NULL,
    note TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS mod_notes_member ON mod_notes (guild_id, user_id);
"""


class CaseError(ValueError):
    """Raised with a message that is safe to show to the moderator."""


def ensure_tables(storage_path: Path) -> None:
    with connect(storage_path) as connection:
        connection.executescript(CREATE_SQL)


def add_case(storage_path: Path, guild_id: int, user_id: int, moderator_id: int, action: str,
             reason: str | None = None, duration: str | None = None) -> int:
    ensure_tables(storage_path)
    with connect(storage_path) as connection:
        cursor = connection.execute(
            "INSERT INTO mod_cases (guild_id, user_id, moderator_id, action, reason, duration) VALUES (?, ?, ?, ?, ?, ?)",
            (int(guild_id), int(user_id), int(moderator_id), action[:60], (reason or "")[:1000], (duration or "")[:60]),
        )
        return int(cursor.lastrowid)


def list_cases(storage_path: Path, guild_id: int, user_id: int | None = None, *, limit: int = 50) -> list[dict[str, Any]]:
    ensure_tables(storage_path)
    sql, params = "SELECT * FROM mod_cases WHERE guild_id = ?", [int(guild_id)]
    if user_id is not None:
        sql, params = sql + " AND user_id = ?", params + [int(user_id)]
    with connect(storage_path) as connection:
        rows = connection.execute(sql + " ORDER BY id DESC LIMIT ?", (*params, int(limit))).fetchall()
    return [dict(row) for row in rows]


def count_cases(storage_path: Path, guild_id: int, user_id: int) -> int:
    ensure_tables(storage_path)
    with connect(storage_path) as connection:
        row = connection.execute("SELECT COUNT(*) FROM mod_cases WHERE guild_id = ? AND user_id = ?", (int(guild_id), int(user_id))).fetchone()
    return int(row[0])


def add_note(storage_path: Path, guild_id: int, user_id: int, author_id: int, note: str) -> int:
    note = (note or "").strip()
    if not note:
        raise CaseError("A note can't be empty.")
    if len(note) > MAX_NOTE_LENGTH:
        raise CaseError(f"Notes can be up to {MAX_NOTE_LENGTH} characters.")
    ensure_tables(storage_path)
    with connect(storage_path) as connection:
        count = connection.execute("SELECT COUNT(*) FROM mod_notes WHERE guild_id = ? AND user_id = ?", (int(guild_id), int(user_id))).fetchone()[0]
        if count >= MAX_NOTES_PER_MEMBER:
            raise CaseError(f"That member already has {MAX_NOTES_PER_MEMBER} notes. Remove one first.")
        cursor = connection.execute(
            "INSERT INTO mod_notes (guild_id, user_id, author_id, note) VALUES (?, ?, ?, ?)",
            (int(guild_id), int(user_id), int(author_id), note),
        )
        return int(cursor.lastrowid)


def list_notes(storage_path: Path, guild_id: int, user_id: int) -> list[dict[str, Any]]:
    ensure_tables(storage_path)
    with connect(storage_path) as connection:
        rows = connection.execute(
            "SELECT * FROM mod_notes WHERE guild_id = ? AND user_id = ? ORDER BY id DESC", (int(guild_id), int(user_id))
        ).fetchall()
    return [dict(row) for row in rows]


def remove_note(storage_path: Path, guild_id: int, note_id: int) -> dict[str, Any] | None:
    """Delete a note, scoped to its guild. Returns the removed row, if any."""
    ensure_tables(storage_path)
    with connect(storage_path) as connection:
        row = connection.execute("SELECT * FROM mod_notes WHERE id = ? AND guild_id = ?", (int(note_id), int(guild_id))).fetchone()
        if row is None:
            return None
        connection.execute("DELETE FROM mod_notes WHERE id = ?", (int(note_id),))
        return dict(row)
