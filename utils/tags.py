"""Custom commands ("tags"): canned replies a server's admins define and
anyone can call up with /tag."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from utils.db import connect

MAX_TAGS_PER_GUILD = 50
MAX_NAME_LENGTH = 32
MAX_CONTENT_LENGTH = 1800
NAME_PATTERN = re.compile(r"^[a-z0-9_-]{1,%d}$" % MAX_NAME_LENGTH)

CREATE_SQL = """
CREATE TABLE IF NOT EXISTS tags (
    guild_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    content TEXT NOT NULL,
    created_by INTEGER NOT NULL,
    uses INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (guild_id, name)
);
"""


class TagError(ValueError):
    """Raised with a message that is safe to show to the user."""


def ensure_table(storage_path: Path) -> None:
    with connect(storage_path) as connection:
        connection.executescript(CREATE_SQL)


def normalize_name(name: str) -> str:
    return (name or "").strip().lower().replace(" ", "-")


def list_tags(storage_path: Path, guild_id: int) -> list[dict[str, Any]]:
    ensure_table(storage_path)
    with connect(storage_path) as connection:
        rows = connection.execute("SELECT * FROM tags WHERE guild_id = ? ORDER BY name", (int(guild_id),)).fetchall()
    return [dict(row) for row in rows]


def get_tag(storage_path: Path, guild_id: int, name: str, *, count_use: bool = False) -> dict[str, Any] | None:
    ensure_table(storage_path)
    key = normalize_name(name)
    with connect(storage_path) as connection:
        row = connection.execute("SELECT * FROM tags WHERE guild_id = ? AND name = ?", (int(guild_id), key)).fetchone()
        if row is not None and count_use:
            connection.execute("UPDATE tags SET uses = uses + 1 WHERE guild_id = ? AND name = ?", (int(guild_id), key))
    return dict(row) if row else None


def set_tag(storage_path: Path, guild_id: int, name: str, content: str, author_id: int) -> tuple[dict[str, Any], bool]:
    """Create a tag or replace its text. Returns (tag, created)."""
    key, content = normalize_name(name), (content or "").strip()
    if not NAME_PATTERN.fullmatch(key):
        raise TagError(f"Tag names use letters, numbers, hyphens and underscores, up to {MAX_NAME_LENGTH} characters.")
    if not content:
        raise TagError("A tag needs some text.")
    if len(content) > MAX_CONTENT_LENGTH:
        raise TagError(f"Tag text can be up to {MAX_CONTENT_LENGTH} characters.")
    ensure_table(storage_path)
    with connect(storage_path) as connection:
        existing = connection.execute("SELECT 1 FROM tags WHERE guild_id = ? AND name = ?", (int(guild_id), key)).fetchone()
        if existing is None:
            count = connection.execute("SELECT COUNT(*) FROM tags WHERE guild_id = ?", (int(guild_id),)).fetchone()[0]
            if count >= MAX_TAGS_PER_GUILD:
                raise TagError(f"This server already has {MAX_TAGS_PER_GUILD} tags. Delete one first.")
            connection.execute("INSERT INTO tags (guild_id, name, content, created_by) VALUES (?, ?, ?, ?)", (int(guild_id), key, content, int(author_id)))
        else:
            connection.execute("UPDATE tags SET content = ?, updated_at = CURRENT_TIMESTAMP WHERE guild_id = ? AND name = ?", (content, int(guild_id), key))
        row = connection.execute("SELECT * FROM tags WHERE guild_id = ? AND name = ?", (int(guild_id), key)).fetchone()
    return dict(row), existing is None


def delete_tag(storage_path: Path, guild_id: int, name: str) -> dict[str, Any] | None:
    ensure_table(storage_path)
    key = normalize_name(name)
    with connect(storage_path) as connection:
        row = connection.execute("SELECT * FROM tags WHERE guild_id = ? AND name = ?", (int(guild_id), key)).fetchone()
        if row is None:
            return None
        connection.execute("DELETE FROM tags WHERE guild_id = ? AND name = ?", (int(guild_id), key))
    return dict(row)
