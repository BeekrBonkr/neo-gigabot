"""Ban appeals: a banned user answers a server's questions on the website and
the server's admins approve (unban) or deny."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from utils.db import connect

DEFAULT_QUESTIONS = (
    "Why were you banned?",
    "Why should the ban be lifted?",
    "Is there anything else the moderators should know?",
)
MAX_QUESTIONS = 8
MAX_QUESTION_LENGTH = 150
MAX_ANSWER_LENGTH = 1000
REAPPEAL_DAYS = 7  # how long after a denial before the same user can appeal again

CREATE_SQL = """
CREATE TABLE IF NOT EXISTS ban_appeals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    user_name TEXT NOT NULL DEFAULT '',
    answers TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL DEFAULT 'pending',
    decided_by INTEGER,
    decision_note TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    decided_at TEXT
);
CREATE INDEX IF NOT EXISTS ban_appeals_guild ON ban_appeals (guild_id, status);
"""


class AppealError(ValueError):
    """Raised with a message that is safe to show to the person appealing."""


def ensure_table(storage_path: Path) -> None:
    with connect(storage_path) as connection:
        connection.executescript(CREATE_SQL)


def questions_for(settings: dict[str, Any]) -> list[str]:
    """The server's own questions (one per line), or the default set."""
    custom = [line.strip()[:MAX_QUESTION_LENGTH] for line in str(settings.get("appeals_questions") or "").splitlines() if line.strip()]
    return custom[:MAX_QUESTIONS] or list(DEFAULT_QUESTIONS)


def _row(row) -> dict[str, Any]:
    record = dict(row)
    record["answers"] = json.loads(record["answers"])
    return record


def latest_appeal(storage_path: Path, guild_id: int, user_id: int) -> dict[str, Any] | None:
    ensure_table(storage_path)
    with connect(storage_path) as connection:
        row = connection.execute(
            "SELECT * FROM ban_appeals WHERE guild_id = ? AND user_id = ? ORDER BY id DESC LIMIT 1", (int(guild_id), int(user_id))
        ).fetchone()
    return _row(row) if row else None


def can_appeal(latest: dict[str, Any] | None, now: datetime | None = None) -> tuple[bool, str]:
    """Whether a new appeal may be sent, given the user's latest one here."""
    if latest is None or latest["status"] == "approved":
        return True, ""
    if latest["status"] == "pending":
        return False, "Your appeal is waiting for the moderators to read it."
    decided = datetime.fromisoformat(str(latest["decided_at"] or latest["created_at"]).replace(" ", "T")).replace(tzinfo=timezone.utc)
    again = decided + timedelta(days=REAPPEAL_DAYS)
    if (now or datetime.now(timezone.utc)) < again:
        return False, f"Your last appeal was denied. You can send another after {again:%B %d, %Y}."
    return True, ""


def submit(storage_path: Path, guild_id: int, user_id: int, user_name: str, questions: list[str], answers: list[str]) -> int:
    cleaned = [str(answer or "").strip()[:MAX_ANSWER_LENGTH] for answer in answers[: len(questions)]]
    cleaned += [""] * (len(questions) - len(cleaned))
    if not any(cleaned):
        raise AppealError("Answer at least one question.")
    allowed, why = can_appeal(latest_appeal(storage_path, guild_id, user_id))
    if not allowed:
        raise AppealError(why)
    pairs = [{"question": question, "answer": answer} for question, answer in zip(questions, cleaned)]
    with connect(storage_path) as connection:
        cursor = connection.execute(
            "INSERT INTO ban_appeals (guild_id, user_id, user_name, answers) VALUES (?, ?, ?, ?)",
            (int(guild_id), int(user_id), str(user_name)[:100], json.dumps(pairs)),
        )
        return int(cursor.lastrowid)


def decide(storage_path: Path, guild_id: int, appeal_id: int, approved: bool, moderator_id: int, note: str = "") -> dict[str, Any] | None:
    """Close a pending appeal. Returns it as decided, or None if there is no
    pending appeal with that ID in this server."""
    ensure_table(storage_path)
    with connect(storage_path) as connection:
        row = connection.execute(
            "SELECT * FROM ban_appeals WHERE id = ? AND guild_id = ? AND status = 'pending'", (int(appeal_id), int(guild_id))
        ).fetchone()
        if row is None:
            return None
        status = "approved" if approved else "denied"
        connection.execute(
            "UPDATE ban_appeals SET status = ?, decided_by = ?, decision_note = ?, decided_at = CURRENT_TIMESTAMP WHERE id = ?",
            (status, int(moderator_id), (note or "").strip()[:500], int(appeal_id)),
        )
        decided = _row(row)
        decided.update(status=status, decided_by=int(moderator_id), decision_note=(note or "").strip()[:500])
        return decided
