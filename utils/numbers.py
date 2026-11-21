"""Telling a number someone typed from text that only looks like one."""

from __future__ import annotations

from typing import Any

# A Discord ID has at most 20 digits; nothing a person types into the bot as a
# count, a position or an ID is longer.
MAX_DIGITS = 20


def is_digits(value: Any) -> bool:
    """Whether `value` is plain digits that int() will accept. `str.isdigit`
    alone is also true for characters int() refuses (a superscript 2), and
    for digit strings long enough that int() raises."""
    text = value if isinstance(value, str) else str(value)
    return text.isascii() and text.isdigit() and len(text) <= MAX_DIGITS
