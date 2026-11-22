"""Forms: a server builds a form on the website, people fill it in, and the
answers are collected for the server's staff.

A form is a list of questions, optionally grouped under section headings, plus
its own rules: who may answer, how often, when it is open, whether answers are
reviewed, and what happens in Discord when one arrives. Ban appeals are one of
these forms, with rules of their own on top (cogs/panel_bridge.py).

This file keeps the forms and the responses and checks everything that goes
into either. It knows nothing about Discord: whoever calls it says who the
person is and what the server's roles, channels and members are called.
"""

from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from utils.db import connect, write_transaction

MAX_FORMS_PER_GUILD = 20
MAX_QUESTIONS = 30
MAX_ITEMS = MAX_QUESTIONS + 10  # questions and section headings together
MAX_RESPONSES_PER_FORM = 5000
MAX_TITLE, MAX_DESCRIPTION = 100, 1000
MAX_QUESTION_TEXT, MAX_HELP_TEXT = 200, 300
MAX_ANSWER, MAX_SHORT_ANSWER = 2000, 200
MAX_OPTIONS, MAX_OPTION_TEXT = 25, 100
MAX_ACTIONS, MAX_ACTION_TEXT = 5, 1000
MAX_NOTE = 500

SECTION = "section"
TEXT_KINDS = ("paragraph", "short")
OPTION_KINDS = ("choice", "checkboxes", "dropdown")
PICKER_KINDS = ("member", "role", "channel")
QUESTION_KINDS = (*TEXT_KINDS, *OPTION_KINDS, "number", "date", "scale", *PICKER_KINDS)
KINDS = (SECTION, *QUESTION_KINDS)

GENERAL, APPEAL = "general", "appeal"
OPEN, CLOSED = "open", "closed"
# A response to a form that is not reviewed is simply "submitted"; one that is reviewed waits as "pending".
SUBMITTED, PENDING, APPROVED, DENIED = "submitted", "pending", "approved", "denied"
ACTION_MOMENTS = ("submit", "approve", "deny")
ACTION_KINDS = ("add_role", "remove_role", "post", "dm", "thread")

DEFAULT_OPTIONS: dict[str, Any] = {
    "audience": "members",  # or "anyone": anybody signed in with Discord who has the link
    "require_role_id": "",
    "min_account_days": 0,
    "min_member_days": 0,
    "multiple": False,  # more than one response from the same person
    "editable": False,  # a response can be changed until it is decided
    "anonymous": False,  # staff are not shown who answered
    "review": False,  # responses wait to be approved or denied
    "notify_outcome": True,  # the person is told when theirs is decided
    "retry_days": 0,  # after a denial, how long before they may answer again
    "channel_id": "",  # where each new response is announced
    "opens_at": "",
    "closes_at": "",
    "max_responses": 0,  # 0: no limit of its own
    "when_full": "close",  # or "rotate": the oldest response makes room
    "keep_days": 0,  # 0: until deleted
    "in_discord": False,  # short forms can be answered in a Discord pop-up
    "actions": [],
}

CREATE_SQL = """
CREATE TABLE IF NOT EXISTS forms (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id INTEGER NOT NULL,
    slug TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'general',
    title TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    items TEXT NOT NULL DEFAULT '[]',
    options TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'closed',
    created_by INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (guild_id, slug)
);
CREATE TABLE IF NOT EXISTS form_responses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    form_id INTEGER NOT NULL,
    guild_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    user_name TEXT NOT NULL DEFAULT '',
    answers TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL DEFAULT 'submitted',
    test INTEGER NOT NULL DEFAULT 0,
    decided_by INTEGER,
    decision_note TEXT NOT NULL DEFAULT '',
    thread_id INTEGER,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    decided_at TEXT
);
CREATE INDEX IF NOT EXISTS form_responses_form ON form_responses (form_id, status, id);
CREATE INDEX IF NOT EXISTS form_responses_user ON form_responses (form_id, user_id);
CREATE TABLE IF NOT EXISTS form_departures (guild_id INTEGER PRIMARY KEY, left_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
"""


class FormError(ValueError):
    """Raised with a message that is safe to show to whoever caused it."""


def ensure_tables(storage_path: Path) -> None:
    with connect(storage_path) as connection:
        connection.executescript(CREATE_SQL)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _moment(text: Any) -> datetime | None:
    """A stored time (SQLite's or ISO) as an aware datetime."""
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(str(text).replace(" ", "T").replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _digits(value: Any) -> str:
    text = str(value or "").strip()
    return text if text.isascii() and text.isdigit() and len(text) <= 20 else ""


def _whole(value: Any, low: int, high: int, default: int = 0) -> int:
    try:
        return max(low, min(high, int(str(value).strip() or default)))
    except (TypeError, ValueError):
        return default


# ---------- what goes into a form ----------

def clean_items(value: Any) -> list[dict[str, Any]]:
    """Check a form's questions and section headings and return them in their
    stored shape. Each keeps its ID, or gets one, so that answers already
    given stay attached to their question when the form is edited."""
    if not isinstance(value, list) or len(value) > MAX_ITEMS:
        raise FormError(f"A form can have up to {MAX_QUESTIONS} questions.")
    taken = {str(item.get("id")) for item in value if isinstance(item, dict) and re.fullmatch(r"q\d{1,4}", str(item.get("id") or ""))}
    counter = max([int(found[1:]) for found in taken] or [0])
    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    for number, raw in enumerate(value, 1):
        if not isinstance(raw, dict):
            raise FormError(f"Item {number} is malformed.")
        kind = str(raw.get("kind") or raw.get("type") or "paragraph")
        if kind not in KINDS:
            raise FormError(f"Item {number} has an unknown type.")
        text = str(raw.get("text") or "").strip()
        if not text or len(text) > MAX_QUESTION_TEXT:
            raise FormError(f"Item {number} needs text of up to {MAX_QUESTION_TEXT} characters.")
        item_id = str(raw.get("id") or "")
        if item_id not in taken or item_id in seen:
            counter += 1
            item_id = f"q{counter}"
        seen.add(item_id)
        item: dict[str, Any] = {"id": item_id, "kind": kind, "text": text, "help": str(raw.get("help") or "").strip()[:MAX_HELP_TEXT]}
        if kind != SECTION:
            item["required"] = bool(raw.get("required"))
        if kind in OPTION_KINDS:
            options: list[str] = []
            for option in raw.get("options") if isinstance(raw.get("options"), list) else []:
                option = str(option).strip()
                if option and option not in options:
                    options.append(option)
            if not 2 <= len(options) <= MAX_OPTIONS or any(len(option) > MAX_OPTION_TEXT for option in options):
                raise FormError(f"\"{text}\" needs 2 to {MAX_OPTIONS} options of up to {MAX_OPTION_TEXT} characters each.")
            item["options"] = options
        if kind == "number":
            low, high = _whole(raw.get("min"), -10**9, 10**9, 0), _whole(raw.get("max"), -10**9, 10**9, 100)
            if low >= high:
                raise FormError(f"\"{text}\" needs a smallest number below its largest.")
            item["min"], item["max"] = low, high
        if kind == "scale":
            low, high = _whole(raw.get("min"), 0, 1, 1), _whole(raw.get("max"), 2, 10, 5)
            item.update(min=low, max=high, low=str(raw.get("low") or "").strip()[:40], high=str(raw.get("high") or "").strip()[:40])
        items.append(item)
    if sum(1 for item in items if item["kind"] != SECTION) > MAX_QUESTIONS:
        raise FormError(f"A form can have up to {MAX_QUESTIONS} questions.")
    return items


def questions(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [item for item in items if item["kind"] != SECTION]


def clean_options(value: Any, current: dict[str, Any] | None = None) -> dict[str, Any]:
    """A form's own rules, checked. Anything not given keeps its current value."""
    given = value if isinstance(value, dict) else {}
    merged = {**DEFAULT_OPTIONS, **(current or {}), **{key: given[key] for key in DEFAULT_OPTIONS if key in given}}
    options: dict[str, Any] = {
        "audience": merged["audience"] if merged["audience"] in ("members", "anyone") else "members",
        "require_role_id": _digits(merged["require_role_id"]),
        "min_account_days": _whole(merged["min_account_days"], 0, 3650),
        "min_member_days": _whole(merged["min_member_days"], 0, 3650),
        "retry_days": _whole(merged["retry_days"], 0, 365),
        "channel_id": _digits(merged["channel_id"]),
        "max_responses": _whole(merged["max_responses"], 0, MAX_RESPONSES_PER_FORM),
        "when_full": merged["when_full"] if merged["when_full"] in ("close", "rotate") else "close",
        "keep_days": _whole(merged["keep_days"], 0, 3650),
    }
    for flag in ("multiple", "editable", "anonymous", "review", "notify_outcome", "in_discord"):
        options[flag] = merged[flag] is True or str(merged[flag]).lower() in ("1", "true", "on", "yes")
    for key in ("opens_at", "closes_at"):
        moment = _moment(merged[key])
        options[key] = moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00") if moment else ""
    if options["opens_at"] and options["closes_at"] and options["opens_at"] >= options["closes_at"]:
        raise FormError("The form has to open before it closes.")
    actions = []
    for raw in merged["actions"] if isinstance(merged["actions"], list) else []:
        if not isinstance(raw, dict):
            continue
        action = {"on": str(raw.get("on")), "do": str(raw.get("do")), "role_id": _digits(raw.get("role_id")),
                  "channel_id": _digits(raw.get("channel_id")), "text": str(raw.get("text") or "").strip()[:MAX_ACTION_TEXT]}
        if action["on"] not in ACTION_MOMENTS or action["do"] not in ACTION_KINDS:
            raise FormError("One of the actions is not something a form can do.")
        if action["do"] in ("add_role", "remove_role") and not action["role_id"]:
            raise FormError("A role action needs a role.")
        if action["do"] in ("post", "thread") and not action["channel_id"]:
            raise FormError("That action needs a channel.")
        if action["do"] in ("post", "dm") and not action["text"]:
            raise FormError("A message action needs its message.")
        actions.append(action)
    if len(actions) > MAX_ACTIONS:
        raise FormError(f"A form can have up to {MAX_ACTIONS} actions.")
    if actions and not options["review"] and any(action["on"] != "submit" for action in actions):
        raise FormError("Actions on approval or denial need the form to be reviewed.")
    options["actions"] = actions
    return options


class Names:
    """What a server's members, roles and channels are called, for the picker
    questions. Each lookup takes an ID and gives the name, or None when the
    server has no such thing. Without a server none of them resolve."""

    def __init__(self, member: Callable[[int], str | None] | None = None, role: Callable[[int], str | None] | None = None,
                 channel: Callable[[int], str | None] | None = None) -> None:
        self.lookups = {"member": member, "role": role, "channel": channel}

    def name(self, kind: str, raw: Any) -> str | None:
        lookup, ident = self.lookups.get(kind), _digits(raw)
        return lookup(int(ident)) if lookup and ident else None


def clean_answers(items: list[dict[str, Any]], answers: Any, names: Names | None = None) -> list[dict[str, Any]]:
    """The answers to a form as they are stored: one entry per question, with
    the question as it read at the time, so a response still makes sense after
    the form is edited. `answers` maps question IDs to what was entered.
    Raises FormError naming the first question that is not answered properly."""
    answers = answers if isinstance(answers, dict) else {}
    names = names or Names()
    stored = []
    for item in questions(items):
        kind, given = item["kind"], answers.get(item["id"])
        value: Any = ""
        shown = ""
        if kind == "checkboxes":
            picked = {str(entry) for entry in (given if isinstance(given, list) else [given])}
            value = [option for option in item["options"] if option in picked]
            shown = ", ".join(value)
        else:
            if isinstance(given, list):
                given = given[0] if given else ""
            text = str(given if given is not None else "").strip()
            if kind in TEXT_KINDS:
                value = shown = text[:MAX_SHORT_ANSWER if kind == "short" else MAX_ANSWER]
            elif kind in ("choice", "dropdown"):
                value = shown = text if text in item["options"] else ""
            elif kind in ("number", "scale") and text:
                try:
                    number = int(text)
                except ValueError:
                    raise FormError(f"\"{item['text']}\" needs a whole number.") from None
                if not item["min"] <= number <= item["max"]:
                    raise FormError(f"\"{item['text']}\" needs a number from {item['min']} to {item['max']}.")
                value, shown = number, str(number)
            elif kind == "date" and text:
                try:
                    value = shown = date.fromisoformat(text).isoformat()
                except ValueError:
                    raise FormError(f"\"{item['text']}\" needs a date.") from None
            elif kind in PICKER_KINDS and text:
                name = names.name(kind, text)
                if name is None:
                    raise FormError(f"\"{item['text']}\" needs a {kind} of this server.")
                value, shown = _digits(text), name
        if item["required"] and shown == "":
            raise FormError(f"Please answer: {item['text']}")
        stored.append({"id": item["id"], "kind": kind, "question": item["text"], "answer": shown, "value": value})
    if not any(entry["answer"] != "" for entry in stored):
        raise FormError("Answer at least one question.")
    return stored


# ---------- forms ----------

def _form(row) -> dict[str, Any]:
    form = dict(row)
    form["items"] = json.loads(form["items"])
    form["options"] = {**DEFAULT_OPTIONS, **json.loads(form["options"])}
    return form


def _slug(title: str, taken: set[str]) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:40] or "form"
    slug, number = base, 1
    while slug in taken:
        number += 1
        slug = f"{base}-{number}"
    return slug


def list_forms(storage_path: Path, guild_id: int) -> list[dict[str, Any]]:
    """A server's forms, the built-in ones first, each with how many responses it has and how many are waiting."""
    ensure_tables(storage_path)
    with connect(storage_path) as connection:
        rows = connection.execute("SELECT * FROM forms WHERE guild_id = ? ORDER BY kind != 'appeal', id", (int(guild_id),)).fetchall()
        counts = {row["form_id"]: (row["total"], row["waiting"]) for row in connection.execute(
            "SELECT form_id, COUNT(*) AS total, SUM(status = 'pending') AS waiting FROM form_responses WHERE guild_id = ? AND test = 0 GROUP BY form_id",
            (int(guild_id),))}
    forms = [_form(row) for row in rows]
    for form in forms:
        form["responses"], form["waiting"] = counts.get(form["id"], (0, 0))[0], int(counts.get(form["id"], (0, 0))[1] or 0)
    return forms


def get_form(storage_path: Path, guild_id: int, form: int | str) -> dict[str, Any] | None:
    """One form of this server, by its ID or by the name in its link."""
    ensure_tables(storage_path)
    column = "id" if isinstance(form, int) or _digits(form) else "slug"
    with connect(storage_path) as connection:
        row = connection.execute(f"SELECT * FROM forms WHERE guild_id = ? AND {column} = ?", (int(guild_id), int(form) if column == "id" else str(form))).fetchone()
    return _form(row) if row else None


def form_by_id(storage_path: Path, form_id: int) -> dict[str, Any] | None:
    ensure_tables(storage_path)
    with connect(storage_path) as connection:
        row = connection.execute("SELECT * FROM forms WHERE id = ?", (int(form_id),)).fetchone()
    return _form(row) if row else None


def create_form(storage_path: Path, guild_id: int, created_by: int, *, title: str = "", template: str = "blank", kind: str = GENERAL,
                items: list[dict[str, Any]] | None = None, description: str | None = None, options: dict[str, Any] | None = None,
                status: str = CLOSED) -> dict[str, Any]:
    """Add a form, closed until its maker opens it, from a template or from the given questions."""
    start = TEMPLATES.get(template) or TEMPLATES["blank"]
    title = (title or start["title"]).strip()[:MAX_TITLE]
    if not title:
        raise FormError("A form needs a title.")
    cleaned = clean_items(items if items is not None else start["items"])
    rules = clean_options({**start.get("options", {}), **(options or {})})
    ensure_tables(storage_path)
    with write_transaction(storage_path) as connection:
        existing = connection.execute("SELECT slug, kind FROM forms WHERE guild_id = ?", (int(guild_id),)).fetchall()
        if sum(1 for row in existing if row["kind"] == GENERAL) >= MAX_FORMS_PER_GUILD and kind == GENERAL:
            raise FormError(f"A server can have up to {MAX_FORMS_PER_GUILD} forms. Delete one first.")
        slug = _slug(title, {row["slug"] for row in existing})
        cursor = connection.execute(
            "INSERT INTO forms (guild_id, slug, kind, title, description, items, options, status, created_by) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (int(guild_id), slug, kind, title, (start["description"] if description is None else description).strip()[:MAX_DESCRIPTION],
             json.dumps(cleaned), json.dumps(rules), status if status in (OPEN, CLOSED) else CLOSED, int(created_by)),
        )
        form_id = int(cursor.lastrowid)
    return get_form(storage_path, guild_id, form_id)


def update_form(storage_path: Path, guild_id: int, form_id: int, *, title: str | None = None, description: str | None = None,
                items: Any = None, options: Any = None, status: str | None = None) -> dict[str, Any]:
    """Change a form. Only what is given changes."""
    form = get_form(storage_path, guild_id, int(form_id))
    if form is None:
        raise FormError("That form no longer exists.")
    if title is not None:
        title = title.strip()[:MAX_TITLE]
        if not title:
            raise FormError("A form needs a title.")
    new_items = clean_items(items) if items is not None else form["items"]
    if status == OPEN and not questions(new_items):
        raise FormError("Add at least one question before opening the form.")
    with connect(storage_path) as connection:
        connection.execute(
            "UPDATE forms SET title = ?, description = ?, items = ?, options = ?, status = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ? AND guild_id = ?",
            (title if title is not None else form["title"],
             description.strip()[:MAX_DESCRIPTION] if description is not None else form["description"],
             json.dumps(new_items), json.dumps(clean_options(options, form["options"]) if options is not None else form["options"]),
             status if status in (OPEN, CLOSED) else form["status"], int(form_id), int(guild_id)),
        )
    return get_form(storage_path, guild_id, int(form_id))


def delete_form(storage_path: Path, guild_id: int, form_id: int) -> dict[str, Any] | None:
    """Delete a form and every response to it. A built-in form cannot be deleted."""
    form = get_form(storage_path, guild_id, int(form_id))
    if form is None:
        return None
    if form["kind"] != GENERAL:
        raise FormError("This form is built in and can't be deleted. Close it instead.")
    with write_transaction(storage_path) as connection:
        connection.execute("DELETE FROM form_responses WHERE form_id = ?", (form["id"],))
        connection.execute("DELETE FROM forms WHERE id = ?", (form["id"],))
    return form


# ---------- responses ----------

def _response(row) -> dict[str, Any]:
    response = dict(row)
    response["answers"] = json.loads(response["answers"])
    return response


def responses_of(storage_path: Path, form_id: int, user_id: int) -> list[dict[str, Any]]:
    """This person's own responses to a form, newest first (never the test ones)."""
    ensure_tables(storage_path)
    with connect(storage_path) as connection:
        rows = connection.execute("SELECT * FROM form_responses WHERE form_id = ? AND user_id = ? AND test = 0 ORDER BY id DESC",
                                  (int(form_id), int(user_id))).fetchall()
    return [_response(row) for row in rows]


def count_responses(storage_path: Path, form_id: int) -> int:
    ensure_tables(storage_path)
    with connect(storage_path) as connection:
        return int(connection.execute("SELECT COUNT(*) FROM form_responses WHERE form_id = ? AND test = 0", (int(form_id),)).fetchone()[0])


def is_open(form: dict[str, Any], total: int, now: datetime | None = None) -> tuple[bool, str]:
    """Whether the form is taking responses at all right now."""
    now, options = now or _now(), form["options"]
    opens, closes = _moment(options["opens_at"]), _moment(options["closes_at"])
    if form["status"] != OPEN:
        return False, "This form is closed."
    if opens and now < opens:
        return False, f"This form opens on {opens:%B %d, %Y at %H:%M} UTC."
    if closes and now >= closes:
        return False, "This form has closed."
    if options["max_responses"] and options["when_full"] == "close" and total >= options["max_responses"]:
        return False, "This form has all the responses it was open for."
    if total >= MAX_RESPONSES_PER_FORM and options["when_full"] == "close":
        return False, "This form is full."
    return True, ""


def can_respond(form: dict[str, Any], mine: list[dict[str, Any]], total: int, now: datetime | None = None) -> tuple[bool, str]:
    """Whether this person, whose earlier responses are `mine` (newest first), may send one now."""
    now, options = now or _now(), form["options"]
    opened, why = is_open(form, total, now)
    if not opened:
        return False, why
    if not mine:
        return True, ""
    latest = mine[0]
    if latest["status"] == PENDING:
        return False, "Your response is waiting to be read."
    if latest["status"] == DENIED and options["retry_days"]:
        again = (_moment(latest["decided_at"] or latest["created_at"]) or now) + timedelta(days=options["retry_days"])
        if now < again:
            return False, f"Your last response was not accepted. You can send another after {again:%B %d, %Y}."
        return True, ""
    if latest["status"] == DENIED and form["kind"] == APPEAL:
        return True, ""
    if form["kind"] == APPEAL and latest["status"] == APPROVED:
        return True, ""  # banned again after a successful appeal
    if not options["multiple"]:
        return False, "You have already answered this form."
    return True, ""


def submit(storage_path: Path, form: dict[str, Any], user_id: int, user_name: str, answers: list[dict[str, Any]], *, test: bool = False) -> int:
    """Store a response (already checked by clean_answers and can_respond).
    A test response, from staff trying the form, is kept apart from real ones."""
    ensure_tables(storage_path)
    status = PENDING if form["options"]["review"] or form["kind"] == APPEAL else SUBMITTED
    with write_transaction(storage_path) as connection:
        if not test:
            total = int(connection.execute("SELECT COUNT(*) FROM form_responses WHERE form_id = ? AND test = 0", (form["id"],)).fetchone()[0])
            limit = min(form["options"]["max_responses"] or MAX_RESPONSES_PER_FORM, MAX_RESPONSES_PER_FORM)
            if total >= limit:
                if form["options"]["when_full"] != "rotate":
                    raise FormError("This form is full.")
                # the oldest responses that are not waiting on anyone make room
                connection.execute(
                    "DELETE FROM form_responses WHERE id IN (SELECT id FROM form_responses WHERE form_id = ? AND test = 0 AND status != 'pending' ORDER BY id LIMIT ?)",
                    (form["id"], total - limit + 1),
                )
        cursor = connection.execute(
            "INSERT INTO form_responses (form_id, guild_id, user_id, user_name, answers, status, test) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (form["id"], form["guild_id"], int(user_id), str(user_name)[:100], json.dumps(answers), status, 1 if test else 0),
        )
        return int(cursor.lastrowid)


def get_response(storage_path: Path, guild_id: int, response_id: int) -> dict[str, Any] | None:
    ensure_tables(storage_path)
    with connect(storage_path) as connection:
        row = connection.execute("SELECT * FROM form_responses WHERE id = ? AND guild_id = ?", (int(response_id), int(guild_id))).fetchone()
    return _response(row) if row else None


def edit_response(storage_path: Path, form: dict[str, Any], response_id: int, user_id: int, answers: list[dict[str, Any]]) -> bool:
    """Replace the answers of this person's own response, while it is still undecided and the form allows editing."""
    if not form["options"]["editable"]:
        raise FormError("Responses to this form can't be changed after they are sent.")
    with connect(storage_path) as connection:
        done = connection.execute(
            "UPDATE form_responses SET answers = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ? AND form_id = ? AND user_id = ? AND status IN ('submitted', 'pending')",
            (json.dumps(answers), int(response_id), form["id"], int(user_id)),
        ).rowcount
    return bool(done)


def withdraw(storage_path: Path, form_id: int, response_id: int, user_id: int) -> dict[str, Any] | None:
    """Delete this person's own response, as long as nobody has decided it."""
    ensure_tables(storage_path)
    with write_transaction(storage_path) as connection:
        row = connection.execute(
            "SELECT * FROM form_responses WHERE id = ? AND form_id = ? AND user_id = ? AND status IN ('submitted', 'pending')",
            (int(response_id), int(form_id), int(user_id)),
        ).fetchone()
        if row is None:
            return None
        connection.execute("DELETE FROM form_responses WHERE id = ?", (int(response_id),))
    return _response(row)


def remove_response(storage_path: Path, guild_id: int, response_id: int) -> dict[str, Any] | None:
    """Delete a response, for the server's staff."""
    found = get_response(storage_path, guild_id, response_id)
    if found is not None:
        with connect(storage_path) as connection:
            connection.execute("DELETE FROM form_responses WHERE id = ?", (int(response_id),))
    return found


def decide(storage_path: Path, guild_id: int, response_id: int, approved: bool, moderator_id: int, note: str = "") -> dict[str, Any] | None:
    """Approve or deny a waiting response. Returns it as decided, or None if it is not waiting."""
    ensure_tables(storage_path)
    note = (note or "").strip()[:MAX_NOTE]
    with write_transaction(storage_path) as connection:
        row = connection.execute("SELECT * FROM form_responses WHERE id = ? AND guild_id = ? AND status = 'pending' AND test = 0",
                                 (int(response_id), int(guild_id))).fetchone()
        if row is None:
            return None
        status = APPROVED if approved else DENIED
        connection.execute(
            "UPDATE form_responses SET status = ?, decided_by = ?, decision_note = ?, decided_at = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (status, int(moderator_id), note, int(response_id)),
        )
    decided = _response(row)
    decided.update(status=status, decided_by=int(moderator_id), decision_note=note)
    return decided


def set_thread(storage_path: Path, response_id: int, thread_id: int) -> None:
    with connect(storage_path) as connection:
        connection.execute("UPDATE form_responses SET thread_id = ? WHERE id = ?", (int(thread_id), int(response_id)))


def list_responses(storage_path: Path, form_id: int, *, status: str = "", limit: int = 50, offset: int = 0, include_tests: bool = True) -> tuple[list[dict[str, Any]], int]:
    """A page of a form's responses, newest first, and how many there are in all."""
    ensure_tables(storage_path)
    clause, params = "form_id = ?", [int(form_id)]
    if status in (SUBMITTED, PENDING, APPROVED, DENIED):
        clause += " AND status = ?"
        params.append(status)
    if not include_tests:
        clause += " AND test = 0"
    with connect(storage_path) as connection:
        total = int(connection.execute(f"SELECT COUNT(*) FROM form_responses WHERE {clause}", params).fetchone()[0])
        rows = connection.execute(f"SELECT * FROM form_responses WHERE {clause} ORDER BY id DESC LIMIT ? OFFSET ?",
                                  (*params, max(1, min(int(limit), MAX_RESPONSES_PER_FORM)), max(0, int(offset)))).fetchall()
    return [_response(row) for row in rows], total


def summary(storage_path: Path, form: dict[str, Any]) -> list[dict[str, Any]]:
    """What the responses add up to, question by question: how many answered,
    how often each option was chosen, and the average of numbers and ratings."""
    responses, _ = list_responses(storage_path, form["id"], limit=MAX_RESPONSES_PER_FORM, include_tests=False)
    by_question: dict[str, list[Any]] = {}
    for response in responses:
        for entry in response["answers"]:
            if entry.get("answer") != "":
                by_question.setdefault(entry["id"], []).append(entry.get("value"))
    result = []
    for item in questions(form["items"]):
        values = by_question.get(item["id"], [])
        line: dict[str, Any] = {"id": item["id"], "kind": item["kind"], "question": item["text"], "answered": len(values), "of": len(responses)}
        if item["kind"] in OPTION_KINDS:
            flat = [option for value in values for option in (value if isinstance(value, list) else [value])]
            line["counts"] = [{"option": option, "n": flat.count(option)} for option in item["options"]]
        elif item["kind"] in ("number", "scale"):
            numbers = [value for value in values if isinstance(value, int)]
            if numbers:
                line.update(average=round(sum(numbers) / len(numbers), 2), low=min(numbers), high=max(numbers))
            if item["kind"] == "scale":
                line["counts"] = [{"option": str(step), "n": numbers.count(step)} for step in range(item["min"], item["max"] + 1)]
        result.append(line)
    return result


# ---------- keeping the tables tidy ----------

def prune(storage_path: Path, now: datetime | None = None) -> int:
    """Remove responses older than their form keeps them for. Ones still waiting for a decision stay."""
    ensure_tables(storage_path)
    removed = 0
    with connect(storage_path) as connection:
        for row in connection.execute("SELECT id, options FROM forms").fetchall():
            keep_days = _whole(json.loads(row["options"]).get("keep_days"), 0, 3650)
            if keep_days:
                removed += connection.execute(
                    "DELETE FROM form_responses WHERE form_id = ? AND status != 'pending' AND created_at < datetime('now', ?)",
                    (row["id"], f"-{keep_days} days"),
                ).rowcount
    return removed


GRACE_DAYS = 30  # how long a server's forms are kept after the bot is removed from it


def bot_left(storage_path: Path, guild_id: int) -> None:
    """Note that the bot is no longer in this server: its forms go after GRACE_DAYS unless it comes back."""
    ensure_tables(storage_path)
    with connect(storage_path) as connection:
        connection.execute("INSERT OR IGNORE INTO form_departures (guild_id) VALUES (?)", (int(guild_id),))


def bot_returned(storage_path: Path, guild_id: int) -> None:
    ensure_tables(storage_path)
    with connect(storage_path) as connection:
        connection.execute("DELETE FROM form_departures WHERE guild_id = ?", (int(guild_id),))


def delete_departed(storage_path: Path, present: set[int]) -> list[int]:
    """Delete the forms and responses of servers the bot left more than
    GRACE_DAYS ago and is still not in. Returns those servers' IDs."""
    ensure_tables(storage_path)
    gone = []
    with write_transaction(storage_path) as connection:
        for row in connection.execute("SELECT guild_id FROM form_departures WHERE left_at < datetime('now', ?)", (f"-{GRACE_DAYS} days",)).fetchall():
            guild_id = int(row["guild_id"])
            if guild_id not in present:
                connection.execute("DELETE FROM form_responses WHERE guild_id = ?", (guild_id,))
                connection.execute("DELETE FROM forms WHERE guild_id = ?", (guild_id,))
                gone.append(guild_id)
            connection.execute("DELETE FROM form_departures WHERE guild_id = ?", (guild_id,))
    return gone


# ---------- starting points ----------

def _q(kind: str, text: str, required: bool = False, **more: Any) -> dict[str, Any]:
    return {"kind": kind, "text": text, "required": required, **more}


TEMPLATES: dict[str, dict[str, Any]] = {
    "blank": {"label": "Blank form", "title": "Untitled form", "description": "", "items": []},
    "staff": {
        "label": "Staff application", "title": "Staff application",
        "description": "Thanks for wanting to help out. Take your time: there are no trick questions.",
        "options": {"review": True},
        "items": [
            _q(SECTION, "About you"),
            _q("short", "What should we call you?", True),
            _q("number", "How old are you?", False, min=13, max=120),
            _q("short", "What time zone are you in?"),
            _q(SECTION, "The role"),
            _q("dropdown", "Which role are you applying for?", True, options=["Moderator", "Helper", "Event host"]),
            _q("paragraph", "Why do you want this role?", True),
            _q("paragraph", "What experience do you have?"),
            _q("scale", "How active are you here in a normal week?", True, min=1, max=5, low="Rarely around", high="Here every day"),
            _q("paragraph", "A member is breaking the rules in chat. What do you do?", True),
        ],
    },
    "event": {
        "label": "Event sign-up", "title": "Event sign-up", "description": "Sign up below and we will be in touch before the event.",
        "items": [
            _q("short", "What name should we put you down as?", True),
            _q("choice", "Can you make it?", True, options=["Yes", "Maybe", "No"]),
            _q("checkboxes", "Which parts do you want to join?", False, options=["Main event", "Warm-up", "After-party"]),
            _q("paragraph", "Anything we should know?"),
        ],
    },
    "feedback": {
        "label": "Feedback survey", "title": "Server feedback", "description": "Tell us how we are doing. It takes about a minute.",
        "options": {"anonymous": True},
        "items": [
            _q("scale", "How happy are you with the server overall?", True, min=1, max=5, low="Not at all", high="Very"),
            _q("checkboxes", "What do you come here for?", False, options=["Chatting", "Events", "Voice", "Help and support", "Something else"]),
            _q("paragraph", "What is the one thing we should change?"),
            _q("paragraph", "What should we keep doing?"),
        ],
    },
    "report": {
        "label": "Report a member", "title": "Report a member",
        "description": "Only the staff see this. Give as much detail as you can.", "options": {"review": True, "multiple": True},
        "items": [
            _q("member", "Who are you reporting?", True),
            _q("channel", "Where did it happen?"),
            _q("date", "When did it happen?"),
            _q("dropdown", "What kind of problem is it?", True, options=["Harassment", "Spam or scam", "Rule breaking", "Something else"]),
            _q("paragraph", "What happened?", True, help="Links to messages help a lot."),
        ],
    },
    "suggestion": {
        "label": "Suggestion", "title": "Suggestion box", "description": "Ideas for the server, big or small.", "options": {"multiple": True},
        "items": [
            _q("short", "Your idea in one line", True),
            _q("paragraph", "Tell us more"),
            _q("choice", "How much does it matter to you?", False, options=["Nice to have", "Would use it a lot", "Really need it"]),
        ],
    },
}
