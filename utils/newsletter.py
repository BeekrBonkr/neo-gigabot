"""Storage for owner newsletters: who opted out, what was sent, and the
per-recipient delivery state that lets an interrupted send resume without
messaging anyone twice."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from utils.db import connect, write_transaction

AUDIENCES = ("owners", "admins")  # "admins" means server owners plus Administrator-permission members
MAX_TITLE_LENGTH = 200
MAX_BODY_LENGTH = 3500

SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS newsletter_optouts (
        user_id INTEGER PRIMARY KEY,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS newsletter_sends (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        finished_at TEXT,
        title TEXT NOT NULL,
        body TEXT NOT NULL,
        audience TEXT NOT NULL,
        state TEXT NOT NULL DEFAULT 'running'
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS newsletter_deliveries (
        send_id INTEGER NOT NULL,
        user_id INTEGER NOT NULL,
        status TEXT NOT NULL DEFAULT 'pending',
        error TEXT NOT NULL DEFAULT '',
        PRIMARY KEY (send_id, user_id)
    )
    """,
)
# running -> done | cancelled | interrupted (bot stopped mid-send; resumable)
RESUMABLE_STATES = ("interrupted", "cancelled")


class NewsletterError(ValueError):
    """User-facing validation error. Message is safe to show as-is."""


def ensure_tables(storage_path: Path) -> None:
    with connect(storage_path) as connection:
        for statement in SCHEMA:
            connection.execute(statement)
        connection.commit()


def validate(title: str, body: str, audience: str) -> tuple[str, str, str]:
    title, body = (title or "").strip(), (body or "").strip()
    if not title or len(title) > MAX_TITLE_LENGTH:
        raise NewsletterError(f"The title must be 1-{MAX_TITLE_LENGTH} characters.")
    if not body or len(body) > MAX_BODY_LENGTH:
        raise NewsletterError(f"The message must be 1-{MAX_BODY_LENGTH} characters.")
    if audience not in AUDIENCES:
        raise NewsletterError("Unknown audience.")
    return title, body, audience


# ---------- opt-outs ----------

def opt_out(storage_path: Path, user_id: int) -> None:
    ensure_tables(storage_path)
    with write_transaction(storage_path) as connection:
        connection.execute("INSERT OR IGNORE INTO newsletter_optouts (user_id) VALUES (?)", (int(user_id),))


def opt_in(storage_path: Path, user_id: int) -> None:
    ensure_tables(storage_path)
    with write_transaction(storage_path) as connection:
        connection.execute("DELETE FROM newsletter_optouts WHERE user_id = ?", (int(user_id),))


def opted_out_ids(storage_path: Path) -> set[int]:
    ensure_tables(storage_path)
    with connect(storage_path) as connection:
        return {int(row[0]) for row in connection.execute("SELECT user_id FROM newsletter_optouts")}


# ---------- sends ----------

def create_send(storage_path: Path, title: str, body: str, audience: str, user_ids: list[int]) -> int:
    ensure_tables(storage_path)
    with write_transaction(storage_path) as connection:
        cursor = connection.execute(
            "INSERT INTO newsletter_sends (title, body, audience) VALUES (?, ?, ?)", (title, body, audience)
        )
        send_id = int(cursor.lastrowid)
        connection.executemany(
            "INSERT OR IGNORE INTO newsletter_deliveries (send_id, user_id) VALUES (?, ?)",
            [(send_id, int(user_id)) for user_id in user_ids],
        )
    return send_id


def pending_user_ids(storage_path: Path, send_id: int) -> list[int]:
    ensure_tables(storage_path)
    with connect(storage_path) as connection:
        rows = connection.execute(
            "SELECT user_id FROM newsletter_deliveries WHERE send_id = ? AND status = 'pending' ORDER BY user_id",
            (int(send_id),),
        ).fetchall()
    return [int(row[0]) for row in rows]


def mark_delivery(storage_path: Path, send_id: int, user_id: int, status: str, error: str = "") -> None:
    with write_transaction(storage_path) as connection:
        connection.execute(
            "UPDATE newsletter_deliveries SET status = ?, error = ? WHERE send_id = ? AND user_id = ?",
            (status, error[:200], int(send_id), int(user_id)),
        )


def set_state(storage_path: Path, send_id: int, state: str) -> None:
    finished = state in ("done", "cancelled")
    with write_transaction(storage_path) as connection:
        connection.execute(
            "UPDATE newsletter_sends SET state = ?, finished_at = CASE WHEN ? THEN CURRENT_TIMESTAMP ELSE finished_at END WHERE id = ?",
            (state, finished, int(send_id)),
        )


def mark_running_as_interrupted(storage_path: Path) -> int:
    """Called at startup: a send still marked running did not survive the
    restart. Its delivery rows are intact, so it can be resumed."""
    ensure_tables(storage_path)
    with write_transaction(storage_path) as connection:
        cursor = connection.execute("UPDATE newsletter_sends SET state = 'interrupted' WHERE state = 'running'")
        return cursor.rowcount


def _with_counts(connection, row) -> dict[str, Any]:
    send = dict(row)
    counts = {"pending": 0, "sent": 0, "failed": 0, "skipped": 0}
    for status, count in connection.execute(
        "SELECT status, COUNT(*) FROM newsletter_deliveries WHERE send_id = ? GROUP BY status", (send["id"],)
    ):
        counts[status] = count
    send["counts"] = counts
    send["total"] = sum(counts.values())
    return send


def get_send(storage_path: Path, send_id: int) -> dict[str, Any] | None:
    ensure_tables(storage_path)
    with connect(storage_path) as connection:
        row = connection.execute("SELECT * FROM newsletter_sends WHERE id = ?", (int(send_id),)).fetchone()
        return _with_counts(connection, row) if row else None


def list_sends(storage_path: Path, limit: int = 30) -> list[dict[str, Any]]:
    ensure_tables(storage_path)
    with connect(storage_path) as connection:
        rows = connection.execute("SELECT * FROM newsletter_sends ORDER BY id DESC LIMIT ?", (int(limit),)).fetchall()
        return [_with_counts(connection, row) for row in rows]


def failure_summary(storage_path: Path, send_id: int) -> list[dict[str, Any]]:
    with connect(storage_path) as connection:
        rows = connection.execute(
            "SELECT error, COUNT(*) AS n FROM newsletter_deliveries WHERE send_id = ? AND status = 'failed' GROUP BY error ORDER BY n DESC",
            (int(send_id),),
        ).fetchall()
    return [{"error": row[0] or "Unknown", "n": row[1]} for row in rows]
