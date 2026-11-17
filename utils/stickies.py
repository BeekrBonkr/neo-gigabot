"""Sticky messages: a message the bot keeps at the bottom of a channel by
re-posting it as people talk. One per channel."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from utils.db import connect

MAX_CONTENT_LENGTH = 1900
HEADER = "__**Stickied Message:**__\n\n"
# A sticky is re-posted after a message once this long has passed, or this
# many messages have been sent, since it was last posted. Slow stickies wait longer.
FAST = (15.0, 5)
SLOW = (35.0, 13)

CREATE_SQL = """
CREATE TABLE IF NOT EXISTS sticky_messages (
    channel_id INTEGER PRIMARY KEY,
    guild_id INTEGER NOT NULL,
    content TEXT NOT NULL,
    embed INTEGER NOT NULL DEFAULT 0,
    slow INTEGER NOT NULL DEFAULT 0,
    active INTEGER NOT NULL DEFAULT 1,
    last_message_id INTEGER,
    created_by INTEGER NOT NULL,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
"""

# Channels with an active sticky, so the message listener can skip every other
# channel without touching the database. Kept in step by the functions below.
_ACTIVE: dict[str, set[int]] = {}


class StickyError(ValueError):
    """Raised with a message that is safe to show to the moderator."""


def clear_cache() -> None:
    _ACTIVE.clear()


def ensure_table(storage_path: Path) -> None:
    with connect(storage_path) as connection:
        connection.executescript(CREATE_SQL)


def active_channels(storage_path: Path) -> set[int]:
    key = str(storage_path)
    if key not in _ACTIVE:
        ensure_table(storage_path)
        with connect(storage_path) as connection:
            _ACTIVE[key] = {int(row[0]) for row in connection.execute("SELECT channel_id FROM sticky_messages WHERE active = 1")}
    return _ACTIVE[key]


def validate(content: str) -> str:
    content = (content or "").replace("\\n", "\n").strip()
    if not content:
        raise StickyError("A sticky needs some text.")
    if len(content) > MAX_CONTENT_LENGTH:
        raise StickyError(f"A sticky can be up to {MAX_CONTENT_LENGTH} characters.")
    return content


def set_sticky(storage_path: Path, guild_id: int, channel_id: int, content: str, created_by: int, *, embed: bool = False, slow: bool = False) -> dict[str, Any]:
    """Stick a message to a channel, or replace the one that is there."""
    content = validate(content)
    ensure_table(storage_path)
    with connect(storage_path) as connection:
        previous = connection.execute("SELECT last_message_id FROM sticky_messages WHERE channel_id = ?", (int(channel_id),)).fetchone()
        connection.execute(
            "INSERT OR REPLACE INTO sticky_messages (channel_id, guild_id, content, embed, slow, active, last_message_id, created_by) VALUES (?, ?, ?, ?, ?, 1, ?, ?)",
            (int(channel_id), int(guild_id), content, int(embed), int(slow), previous[0] if previous else None, int(created_by)),
        )
    active_channels(storage_path).add(int(channel_id))
    return get(storage_path, channel_id)


def get(storage_path: Path, channel_id: int) -> dict[str, Any] | None:
    ensure_table(storage_path)
    with connect(storage_path) as connection:
        row = connection.execute("SELECT * FROM sticky_messages WHERE channel_id = ?", (int(channel_id),)).fetchone()
    return dict(row) if row else None


def set_active(storage_path: Path, channel_id: int, active: bool) -> dict[str, Any] | None:
    sticky = get(storage_path, channel_id)
    if sticky is None:
        return None
    with connect(storage_path) as connection:
        connection.execute("UPDATE sticky_messages SET active = ? WHERE channel_id = ?", (int(active), int(channel_id)))
    (active_channels(storage_path).add if active else active_channels(storage_path).discard)(int(channel_id))
    return {**sticky, "active": int(active)}


def remove(storage_path: Path, channel_id: int) -> dict[str, Any] | None:
    sticky = get(storage_path, channel_id)
    if sticky is None:
        return None
    with connect(storage_path) as connection:
        connection.execute("DELETE FROM sticky_messages WHERE channel_id = ?", (int(channel_id),))
    active_channels(storage_path).discard(int(channel_id))
    return sticky


def set_last_message(storage_path: Path, channel_id: int, message_id: int | None) -> None:
    with connect(storage_path) as connection:
        connection.execute("UPDATE sticky_messages SET last_message_id = ? WHERE channel_id = ?", (message_id, int(channel_id)))


def list_for_guild(storage_path: Path, guild_id: int) -> list[dict[str, Any]]:
    ensure_table(storage_path)
    with connect(storage_path) as connection:
        return [dict(row) for row in connection.execute("SELECT * FROM sticky_messages WHERE guild_id = ? ORDER BY channel_id", (int(guild_id),))]


class Pacer:
    """Decides, per channel, whether a new message should bring the sticky
    back down: enough time or enough messages since it was last posted."""

    def __init__(self) -> None:
        self._state: dict[int, tuple[float, int]] = {}  # channel -> (last post time, messages since)

    def should_post(self, channel_id: int, slow: bool, now: float | None = None) -> bool:
        now = time.monotonic() if now is None else now
        seconds, messages = SLOW if slow else FAST
        last, count = self._state.get(channel_id, (float("-inf"), 0))
        count += 1
        if now - last >= seconds or count >= messages:
            self._state[channel_id] = (now, 0)
            return True
        self._state[channel_id] = (last, count)
        return False

    def posted(self, channel_id: int, now: float | None = None) -> None:
        self._state[channel_id] = (time.monotonic() if now is None else now, 0)

    def forget(self, channel_id: int) -> None:
        self._state.pop(channel_id, None)
