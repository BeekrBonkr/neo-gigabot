"""When each command was added, so the website can mark the recent ones as new.

The dates live in command_dates.json, one line per command: its full name
("animate boom", or a right-click command's own name) and the day it first
shipped. A new command needs a line there; tests/test_command_dates.py fails
until it has one."""

from __future__ import annotations

import datetime
import functools
import json
from pathlib import Path

DATES_FILE = Path(__file__).with_name("command_dates.json")
NEW_FOR_DAYS = 30  # how long a command counts as new; the website uses the same figure


@functools.lru_cache(maxsize=1)
def _dates() -> dict[str, str]:
    return json.loads(DATES_FILE.read_text(encoding="utf-8"))


def added_on(name: str) -> str | None:
    """The day a command was added, as YYYY-MM-DD, or None if it was never recorded."""
    return _dates().get(name)


def is_new(name: str, today: datetime.date | None = None) -> bool:
    """Whether the command was added within the last NEW_FOR_DAYS days."""
    added = added_on(name)
    if added is None:
        return False
    today = today or datetime.date.today()
    return today - datetime.timedelta(days=NEW_FOR_DAYS) < datetime.date.fromisoformat(added) <= today
