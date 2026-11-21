"""A searchable copy of the activity log, for the website's log viewer.

The Discord log channel stays the primary record. This keeps the same events
for a limited time (the server's logging_keep_days) so admins can search them.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from utils.db import connect

MAX_ROWS_PER_GUILD = 5000
PRUNE_EVERY = 100  # inserts between clean-ups

CREATE_SQL = """
CREATE TABLE IF NOT EXISTS activity_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id INTEGER NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    title TEXT NOT NULL,
    body TEXT NOT NULL DEFAULT '{}',
    search TEXT NOT NULL DEFAULT '',
    user_id INTEGER,
    channel_id INTEGER
);
CREATE INDEX IF NOT EXISTS activity_log_guild ON activity_log (guild_id, id);
"""

_inserts = 0


def ensure_table(storage_path: Path) -> None:
    with connect(storage_path) as connection:
        connection.executescript(CREATE_SQL)


def record(storage_path: Path, guild_id: int, *, title: str, description: str = "", fields: list[tuple[str, str]] | None = None,
           footer: str = "", user_id: int | None = None, channel_id: int | None = None, keep_days: int = 30) -> None:
    """Store one event. `keep_days` of 0 stores nothing."""
    global _inserts
    if keep_days <= 0:
        return
    fields = [(str(name), str(value)) for name, value in (fields or [])]
    body = {"description": description or "", "fields": fields, "footer": footer or ""}
    search = " ".join([title, description or "", footer or ""] + [f"{name} {value}" for name, value in fields]).lower()[:4000]
    ensure_table(storage_path)
    with connect(storage_path) as connection:
        connection.execute(
            "INSERT INTO activity_log (guild_id, title, body, search, user_id, channel_id) VALUES (?, ?, ?, ?, ?, ?)",
            (int(guild_id), title[:200], json.dumps(body), search, user_id, channel_id),
        )
        _inserts += 1
        if _inserts % PRUNE_EVERY == 0:
            _prune(connection, int(guild_id), keep_days)


def _prune(connection, guild_id: int, keep_days: int) -> None:
    connection.execute("DELETE FROM activity_log WHERE guild_id = ? AND created_at < datetime('now', ?)", (guild_id, f"-{int(keep_days)} days"))
    connection.execute(
        "DELETE FROM activity_log WHERE guild_id = ? AND id <= COALESCE("
        "(SELECT id FROM activity_log WHERE guild_id = ? ORDER BY id DESC LIMIT 1 OFFSET ?), 0)",
        (guild_id, guild_id, MAX_ROWS_PER_GUILD),
    )


def prune_all(storage_path: Path, keep_days_for) -> int:
    """Apply every server's own retention, quiet servers included.
    `keep_days_for(guild_id)` gives the days to keep (0: keep nothing, as when
    logging is off). Returns how many rows went."""
    ensure_table(storage_path)
    with connect(storage_path) as connection:
        before = connection.total_changes
        for (guild_id,) in connection.execute("SELECT DISTINCT guild_id FROM activity_log").fetchall():
            keep_days = int(keep_days_for(guild_id))
            if keep_days <= 0:
                connection.execute("DELETE FROM activity_log WHERE guild_id = ?", (guild_id,))
            else:
                _prune(connection, guild_id, keep_days)
        return connection.total_changes - before


def prune(storage_path: Path, guild_id: int, keep_days: int) -> None:
    ensure_table(storage_path)
    with connect(storage_path) as connection:
        if keep_days <= 0:
            connection.execute("DELETE FROM activity_log WHERE guild_id = ?", (int(guild_id),))
        else:
            _prune(connection, int(guild_id), keep_days)


def search(storage_path: Path, guild_id: int, *, query: str = "", limit: int = 100) -> list[dict[str, Any]]:
    ensure_table(storage_path)
    sql, params = "SELECT * FROM activity_log WHERE guild_id = ?", [int(guild_id)]
    for word in query.lower().split()[:6]:
        sql, params = sql + " AND search LIKE ? ESCAPE '\\'", params + ["%" + word.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"]
    with connect(storage_path) as connection:
        rows = connection.execute(sql + " ORDER BY id DESC LIMIT ?", (*params, int(limit))).fetchall()
    return [{**dict(row), "body": json.loads(row["body"])} for row in rows]
