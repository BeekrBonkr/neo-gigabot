"""Scheduled actions: something the bot does at a set time, optionally
repeating. Posting a message is the common one; the others lock or unlock a
channel, set its slowmode, clear it out, or give or take a role."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from utils.db import connect

MAX_SCHEDULES_PER_GUILD = 20
MAX_CONTENT_LENGTH = 2000
MIN_INTERVAL_MINUTES = 10
MAX_INTERVAL_MINUTES = 60 * 24 * 366
MAX_AHEAD = timedelta(days=366)
MAX_SLOWMODE_SECONDS = 21600
MAX_PURGE_COUNT = 500
# What each action needs: a channel, and whether the text is the point
# ("required"), an optional note posted alongside ("optional") or unused.
ACTIONS: dict[str, dict[str, Any]] = {
    "message": {"label": "Post a message", "channel": True, "content": "required"},
    "lock": {"label": "Lock a channel", "channel": True, "content": "optional"},
    "unlock": {"label": "Unlock a channel", "channel": True, "content": "optional"},
    "slowmode": {"label": "Set slowmode", "channel": True, "content": "optional"},
    "purge": {"label": "Clear recent messages", "channel": True, "content": "optional"},
    "role_add": {"label": "Give a role", "channel": False, "content": "unused"},
    "role_remove": {"label": "Take a role away", "channel": False, "content": "unused"},
}

CREATE_SQL = """
CREATE TABLE IF NOT EXISTS scheduled_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id INTEGER NOT NULL,
    channel_id INTEGER NOT NULL,
    content TEXT NOT NULL,
    next_run_at TEXT NOT NULL,
    interval_minutes INTEGER NOT NULL DEFAULT 0,
    created_by INTEGER NOT NULL,
    last_run_at TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS scheduled_messages_due ON scheduled_messages (next_run_at);
"""


class ScheduleError(ValueError):
    """Raised with a message that is safe to show to the admin."""


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def parse_utc(value: str) -> datetime | None:
    """A stored or submitted UTC timestamp; None if it isn't one."""
    try:
        moment = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00").replace(" ", "T"))
    except ValueError:
        return None
    return moment.replace(tzinfo=timezone.utc) if moment.tzinfo is None else moment.astimezone(timezone.utc)


def ensure_table(storage_path: Path) -> None:
    with connect(storage_path) as connection:
        connection.executescript(CREATE_SQL)
        columns = {row[1] for row in connection.execute("PRAGMA table_info(scheduled_messages)")}
        if "action" not in columns:
            connection.execute("ALTER TABLE scheduled_messages ADD COLUMN action TEXT NOT NULL DEFAULT 'message'")
        if "options" not in columns:
            connection.execute("ALTER TABLE scheduled_messages ADD COLUMN options TEXT NOT NULL DEFAULT '{}'")


def _entry(row) -> dict[str, Any]:
    entry = dict(row)
    try:
        entry["options"] = json.loads(entry.get("options") or "{}")
    except ValueError:
        entry["options"] = {}
    return entry


def clean_options(action: str, options: dict[str, Any] | None) -> dict[str, int]:
    """The options an action takes, checked. Raises ScheduleError otherwise."""
    options = options or {}

    def number(key: str, low: int, high: int, what: str) -> int:
        raw = str(options.get(key, "")).strip()
        if not raw.isdigit() or not low <= int(raw) <= high:
            raise ScheduleError(what)
        return int(raw)

    if action == "slowmode":
        return {"seconds": number("seconds", 0, MAX_SLOWMODE_SECONDS, f"Slowmode is a number of seconds from 0 (off) to {MAX_SLOWMODE_SECONDS}.")}
    if action == "purge":
        return {"count": number("count", 1, MAX_PURGE_COUNT, f"Choose how many messages to clear, from 1 to {MAX_PURGE_COUNT}.")}
    if action in ("role_add", "role_remove"):
        return {"role_id": number("role_id", 1, 2**63, "Choose a role."), "user_id": number("user_id", 1, 2**63, "Enter the member's user ID.")}
    return {}


def add(storage_path: Path, guild_id: int, channel_id: int, content: str, run_at: datetime, interval_minutes: int, created_by: int,
        *, action: str = "message", options: dict[str, Any] | None = None, now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    if action not in ACTIONS:
        raise ScheduleError("Unknown action.")
    cleaned = clean_options(action, options)
    content = (content or "").strip() if ACTIONS[action]["content"] != "unused" else ""
    if not ACTIONS[action]["channel"]:
        channel_id = 0
    if not content and ACTIONS[action]["content"] == "required":
        raise ScheduleError("The message can't be empty.")
    if len(content) > MAX_CONTENT_LENGTH:
        raise ScheduleError(f"Messages can be up to {MAX_CONTENT_LENGTH} characters.")
    if interval_minutes and not MIN_INTERVAL_MINUTES <= interval_minutes <= MAX_INTERVAL_MINUTES:
        raise ScheduleError(f"A repeating action has to repeat at least every {MIN_INTERVAL_MINUTES} minutes and at most once a year.")
    if run_at <= now - timedelta(minutes=1):
        raise ScheduleError("That time has already passed.")
    if run_at > now + MAX_AHEAD:
        raise ScheduleError("Actions can be scheduled up to a year ahead.")
    ensure_table(storage_path)
    with connect(storage_path) as connection:
        count = connection.execute("SELECT COUNT(*) FROM scheduled_messages WHERE guild_id = ?", (int(guild_id),)).fetchone()[0]
        if count >= MAX_SCHEDULES_PER_GUILD:
            raise ScheduleError(f"This server already has {MAX_SCHEDULES_PER_GUILD} scheduled actions. Remove one first.")
        cursor = connection.execute(
            "INSERT INTO scheduled_messages (guild_id, channel_id, content, next_run_at, interval_minutes, created_by, action, options) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (int(guild_id), int(channel_id), content, _iso(run_at), int(interval_minutes or 0), int(created_by), action, json.dumps(cleaned)),
        )
        row = connection.execute("SELECT * FROM scheduled_messages WHERE id = ?", (cursor.lastrowid,)).fetchone()
    return _entry(row)


def list_for_guild(storage_path: Path, guild_id: int) -> list[dict[str, Any]]:
    ensure_table(storage_path)
    with connect(storage_path) as connection:
        rows = connection.execute("SELECT * FROM scheduled_messages WHERE guild_id = ? ORDER BY next_run_at", (int(guild_id),)).fetchall()
    return [_entry(row) for row in rows]


def remove(storage_path: Path, guild_id: int, schedule_id: int) -> dict[str, Any] | None:
    ensure_table(storage_path)
    with connect(storage_path) as connection:
        row = connection.execute("SELECT * FROM scheduled_messages WHERE id = ? AND guild_id = ?", (int(schedule_id), int(guild_id))).fetchone()
        if row is None:
            return None
        connection.execute("DELETE FROM scheduled_messages WHERE id = ?", (int(schedule_id),))
    return _entry(row)


def due(storage_path: Path, now: datetime | None = None) -> list[dict[str, Any]]:
    ensure_table(storage_path)
    with connect(storage_path) as connection:
        rows = connection.execute("SELECT * FROM scheduled_messages WHERE next_run_at <= ? ORDER BY next_run_at", (_iso(now or datetime.now(timezone.utc)),)).fetchall()
    return [_entry(row) for row in rows]


def finish_run(storage_path: Path, entry: dict[str, Any], now: datetime | None = None) -> datetime | None:
    """Mark one run as done: a one-off is removed, a repeating message moves
    to its next time after now (missed runs are skipped, not sent in a burst).
    Returns the next run time, or None when the message is finished."""
    now = now or datetime.now(timezone.utc)
    interval = int(entry["interval_minutes"] or 0)
    with connect(storage_path) as connection:
        if interval <= 0:
            connection.execute("DELETE FROM scheduled_messages WHERE id = ?", (entry["id"],))
            return None
        upcoming = parse_utc(entry["next_run_at"]) or now
        step = timedelta(minutes=interval)
        if upcoming <= now:
            upcoming += step * (int((now - upcoming) / step) + 1)
        connection.execute("UPDATE scheduled_messages SET next_run_at = ?, last_run_at = ? WHERE id = ?", (_iso(upcoming), _iso(now), entry["id"]))
        return upcoming
