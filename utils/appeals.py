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
MAX_SHORT_ANSWER_LENGTH = 200
MAX_OPTIONS = 10
MAX_OPTION_LENGTH = 100
REAPPEAL_DAYS = 7  # default wait after a denial before the same user can appeal again
# paragraph and short are typed answers; choice picks one option, checkboxes any number.
QUESTION_TYPES = ("paragraph", "short", "choice", "checkboxes")

CREATE_SQL = """
CREATE TABLE IF NOT EXISTS ban_appeals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    user_name TEXT NOT NULL DEFAULT '',
    answers TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL DEFAULT 'pending',
    test INTEGER NOT NULL DEFAULT 0,
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
        if "test" not in {row[1] for row in connection.execute("PRAGMA table_info(ban_appeals)")}:
            connection.execute("ALTER TABLE ban_appeals ADD COLUMN test INTEGER NOT NULL DEFAULT 0")


def _question(text: str, kind: str = "paragraph", options: list[str] | None = None, required: bool = False) -> dict[str, Any]:
    return {"text": text, "type": kind, "options": options or [], "required": required}


def clean_form(value: Any) -> list[dict[str, Any]]:
    """Check a server's own appeal form (the appeals_form setting) and return
    it in its stored shape. Raises AppealError saying what is wrong."""
    if not isinstance(value, list) or len(value) > MAX_QUESTIONS:
        raise AppealError(f"An appeal form can have up to {MAX_QUESTIONS} questions.")
    form = []
    for number, item in enumerate(value, 1):
        if not isinstance(item, dict):
            raise AppealError(f"Question {number} is malformed.")
        text, kind = str(item.get("text") or "").strip(), str(item.get("type") or "paragraph")
        if not text or len(text) > MAX_QUESTION_LENGTH:
            raise AppealError(f"Question {number} needs text of up to {MAX_QUESTION_LENGTH} characters.")
        if kind not in QUESTION_TYPES:
            raise AppealError(f"Question {number} has an unknown type.")
        options: list[str] = []
        if kind in ("choice", "checkboxes"):
            raw = item.get("options") if isinstance(item.get("options"), list) else []
            for option in raw:
                option = str(option).strip()
                if option and option not in options:
                    options.append(option)
            if not 2 <= len(options) <= MAX_OPTIONS or any(len(option) > MAX_OPTION_LENGTH for option in options):
                raise AppealError(f"Question {number} needs 2 to {MAX_OPTIONS} options of up to {MAX_OPTION_LENGTH} characters each.")
        form.append(_question(text, kind, options, bool(item.get("required"))))
    return form


def questions_for(settings: dict[str, Any]) -> list[dict[str, Any]]:
    """The questions a server asks: its own form, else the older one-per-line
    text setting, else the default set."""
    form = settings.get("appeals_form")
    if isinstance(form, list) and form:
        try:
            return clean_form(form)
        except AppealError:
            pass  # a hand-edited setting that doesn't hold up: fall through
    lines = [line.strip()[:MAX_QUESTION_LENGTH] for line in str(settings.get("appeals_questions") or "").splitlines() if line.strip()]
    texts = lines[:MAX_QUESTIONS] or list(DEFAULT_QUESTIONS)
    return [_question(text, required=index == 0) for index, text in enumerate(texts)]


def reappeal_days(settings: dict[str, Any]) -> int:
    try:
        return max(0, int(settings.get("appeals_reappeal_days", REAPPEAL_DAYS)))
    except (TypeError, ValueError):
        return REAPPEAL_DAYS


def clean_answer(question: dict[str, Any], answer: Any) -> str:
    """One answer as stored: text cut to length, or only options that exist."""
    kind = question["type"]
    if kind == "checkboxes":
        picked = answer if isinstance(answer, list) else [answer]
        return ", ".join(option for option in question["options"] if option in {str(p) for p in picked})
    if isinstance(answer, list):
        answer = answer[0] if answer else ""
    text = str(answer or "").strip()
    if kind == "choice":
        return text if text in question["options"] else ""
    return text[:MAX_SHORT_ANSWER_LENGTH if kind == "short" else MAX_ANSWER_LENGTH]


def _row(row) -> dict[str, Any]:
    record = dict(row)
    record["answers"] = json.loads(record["answers"])
    return record


def latest_appeal(storage_path: Path, guild_id: int, user_id: int) -> dict[str, Any] | None:
    ensure_table(storage_path)
    with connect(storage_path) as connection:
        row = connection.execute(
            "SELECT * FROM ban_appeals WHERE guild_id = ? AND user_id = ? AND test = 0 ORDER BY id DESC LIMIT 1", (int(guild_id), int(user_id))
        ).fetchone()
    return _row(row) if row else None


def can_appeal(latest: dict[str, Any] | None, now: datetime | None = None, wait_days: int = REAPPEAL_DAYS) -> tuple[bool, str]:
    """Whether a new appeal may be sent, given the user's latest one here."""
    if latest is None or latest["status"] == "approved":
        return True, ""
    if latest["status"] == "pending":
        return False, "Your appeal is waiting for the moderators to read it."
    decided = datetime.fromisoformat(str(latest["decided_at"] or latest["created_at"]).replace(" ", "T")).replace(tzinfo=timezone.utc)
    again = decided + timedelta(days=wait_days)
    if (now or datetime.now(timezone.utc)) < again:
        return False, f"Your last appeal was denied. You can send another after {again:%B %d, %Y}."
    return True, ""


def submit(storage_path: Path, guild_id: int, user_id: int, user_name: str, questions: list[dict[str, Any] | str], answers: list[Any],
           *, wait_days: int = REAPPEAL_DAYS, test: bool = False) -> int:
    """Store an appeal. A test appeal (an admin trying the form out) skips the
    one-at-a-time rule and never counts as the user's appeal."""
    questions = [_question(q, required=False) if isinstance(q, str) else q for q in questions]
    given = list(answers[: len(questions)]) + [""] * (len(questions) - len(answers))
    cleaned = [clean_answer(question, answer) for question, answer in zip(questions, given)]
    for question, answer in zip(questions, cleaned):
        if question["required"] and not answer:
            raise AppealError(f"Please answer: {question['text']}")
    if not any(cleaned):
        raise AppealError("Answer at least one question.")
    ensure_table(storage_path)
    if not test:
        allowed, why = can_appeal(latest_appeal(storage_path, guild_id, user_id), wait_days=wait_days)
        if not allowed:
            raise AppealError(why)
    pairs = [{"question": question["text"], "answer": answer} for question, answer in zip(questions, cleaned)]
    with connect(storage_path) as connection:
        cursor = connection.execute(
            "INSERT INTO ban_appeals (guild_id, user_id, user_name, answers, test) VALUES (?, ?, ?, ?, ?)",
            (int(guild_id), int(user_id), str(user_name)[:100], json.dumps(pairs), 1 if test else 0),
        )
        return int(cursor.lastrowid)


def remove_test(storage_path: Path, guild_id: int, appeal_id: int) -> dict[str, Any] | None:
    """Delete a test appeal. Real appeals are never deleted this way."""
    ensure_table(storage_path)
    with connect(storage_path) as connection:
        row = connection.execute("SELECT * FROM ban_appeals WHERE id = ? AND guild_id = ? AND test = 1", (int(appeal_id), int(guild_id))).fetchone()
        if row is None:
            return None
        connection.execute("DELETE FROM ban_appeals WHERE id = ?", (int(appeal_id),))
    return _row(row)


def decide(storage_path: Path, guild_id: int, appeal_id: int, approved: bool, moderator_id: int, note: str = "") -> dict[str, Any] | None:
    """Close a pending appeal. Returns it as decided, or None if there is no
    pending appeal with that ID in this server."""
    ensure_table(storage_path)
    with connect(storage_path) as connection:
        row = connection.execute(
            "SELECT * FROM ban_appeals WHERE id = ? AND guild_id = ? AND status = 'pending' AND test = 0", (int(appeal_id), int(guild_id))
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
