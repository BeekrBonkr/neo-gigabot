"""When each command was added, so the website can mark the recent ones as new.

The dates live in command_dates.json, one line per command: its full name
("animate boom", or a right-click command's own name) and the day it first
shipped. A new command needs a line there; tests/test_command_dates.py fails
until it has one."""

from __future__ import annotations

import functools
import json
from pathlib import Path

DATES_FILE = Path(__file__).with_name("command_dates.json")


@functools.lru_cache(maxsize=1)
def _dates() -> dict[str, str]:
    return json.loads(DATES_FILE.read_text(encoding="utf-8"))


def added_on(name: str) -> str | None:
    """The day a command was added, as YYYY-MM-DD, or None if it was never recorded."""
    return _dates().get(name)
