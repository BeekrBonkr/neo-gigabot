"""Member suggestions: posted to a channel, voted on with buttons, and
approved or denied by staff."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from utils.db import connect

MAX_SUGGESTION_LENGTH = 1000
STATUSES = ("open", "approved", "denied")

CREATE_SQL = """
CREATE TABLE IF NOT EXISTS suggestions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id INTEGER NOT NULL,
    number INTEGER NOT NULL,
    channel_id INTEGER NOT NULL DEFAULT 0,
    message_id INTEGER NOT NULL DEFAULT 0,
    author_id INTEGER NOT NULL,
    text TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open',
    votes TEXT NOT NULL DEFAULT '{}',
    decided_by INTEGER,
    decision_reason TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (guild_id, number)
);
CREATE INDEX IF NOT EXISTS suggestions_message ON suggestions (message_id);
"""


def ensure_table(storage_path: Path) -> None:
    with connect(storage_path) as connection:
        connection.executescript(CREATE_SQL)


def _row(row) -> dict[str, Any]:
    record = dict(row)
    record["votes"] = {str(k): int(v) for k, v in json.loads(record["votes"]).items()}
    return record


def tally(suggestion: dict[str, Any]) -> tuple[int, int]:
    votes = suggestion["votes"].values()
    return sum(1 for v in votes if v > 0), sum(1 for v in votes if v < 0)


def create(storage_path: Path, guild_id: int, author_id: int, text: str) -> dict[str, Any]:
    """Store a suggestion with the next number for its server."""
    ensure_table(storage_path)
    with connect(storage_path) as connection:
        number = connection.execute("SELECT COALESCE(MAX(number), 0) + 1 FROM suggestions WHERE guild_id = ?", (int(guild_id),)).fetchone()[0]
        cursor = connection.execute(
            "INSERT INTO suggestions (guild_id, number, author_id, text) VALUES (?, ?, ?, ?)",
            (int(guild_id), number, int(author_id), text.strip()[:MAX_SUGGESTION_LENGTH]),
        )
        row = connection.execute("SELECT * FROM suggestions WHERE id = ?", (cursor.lastrowid,)).fetchone()
    return _row(row)


def set_message(storage_path: Path, suggestion_id: int, channel_id: int, message_id: int) -> None:
    with connect(storage_path) as connection:
        connection.execute("UPDATE suggestions SET channel_id = ?, message_id = ? WHERE id = ?", (int(channel_id), int(message_id), int(suggestion_id)))


def delete(storage_path: Path, suggestion_id: int) -> None:
    with connect(storage_path) as connection:
        connection.execute("DELETE FROM suggestions WHERE id = ?", (int(suggestion_id),))


def by_message(storage_path: Path, message_id: int) -> dict[str, Any] | None:
    ensure_table(storage_path)
    with connect(storage_path) as connection:
        row = connection.execute("SELECT * FROM suggestions WHERE message_id = ?", (int(message_id),)).fetchone()
    return _row(row) if row else None


def by_number(storage_path: Path, guild_id: int, number: int) -> dict[str, Any] | None:
    ensure_table(storage_path)
    with connect(storage_path) as connection:
        row = connection.execute("SELECT * FROM suggestions WHERE guild_id = ? AND number = ?", (int(guild_id), int(number))).fetchone()
    return _row(row) if row else None


def vote(storage_path: Path, message_id: int, user_id: int, value: int) -> tuple[dict[str, Any] | None, str]:
    """Cast, switch or (by pressing the same button again) withdraw a vote.
    Returns the updated suggestion and what happened: added, switched,
    removed, or closed when it has already been decided."""
    ensure_table(storage_path)
    with connect(storage_path) as connection:
        row = connection.execute("SELECT * FROM suggestions WHERE message_id = ?", (int(message_id),)).fetchone()
        if row is None:
            return None, "missing"
        suggestion = _row(row)
        if suggestion["status"] != "open":
            return suggestion, "closed"
        key, previous = str(int(user_id)), suggestion["votes"].get(str(int(user_id)))
        if previous == value:
            del suggestion["votes"][key]
            outcome = "removed"
        else:
            suggestion["votes"][key] = value
            outcome = "switched" if previous else "added"
        connection.execute("UPDATE suggestions SET votes = ? WHERE id = ?", (json.dumps(suggestion["votes"]), suggestion["id"]))
    return suggestion, outcome


def decide(storage_path: Path, guild_id: int, number: int, status: str, moderator_id: int, reason: str = "") -> dict[str, Any] | None:
    """Approve or deny (or change the decision on) a suggestion."""
    if status not in ("approved", "denied"):
        raise ValueError(status)
    ensure_table(storage_path)
    with connect(storage_path) as connection:
        row = connection.execute("SELECT * FROM suggestions WHERE guild_id = ? AND number = ?", (int(guild_id), int(number))).fetchone()
        if row is None:
            return None
        connection.execute(
            "UPDATE suggestions SET status = ?, decided_by = ?, decision_reason = ? WHERE id = ?",
            (status, int(moderator_id), (reason or "").strip()[:500], row["id"]),
        )
        suggestion = _row(row)
        suggestion.update(status=status, decided_by=int(moderator_id), decision_reason=(reason or "").strip()[:500])
        return suggestion
