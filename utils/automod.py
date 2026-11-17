"""Automod rule checks, kept free of Discord objects so they can be tested.

A rule is on when its `automod_<rule>_action` setting isn't "off". Every rule
that fires deletes the message; "warn" and "mute" add to that.
"""

from __future__ import annotations

import re
import time
from collections import deque
from typing import Any

INVITE_PATTERN = re.compile(r"(?:discord(?:app)?\.com/invite|discord\.gg|discord\.me|dsc\.gg)/[\w-]+", re.IGNORECASE)
SPAM_WINDOW_SECONDS = 8.0
RULES = ("invites", "words", "mentions", "spam")
RULE_LABELS = {
    "invites": "posting invite links",
    "words": "using a word that isn't allowed here",
    "mentions": "mentioning too many people at once",
    "spam": "sending messages too quickly",
}


def banned_words(raw: str) -> list[str]:
    """The banned word list as entered (commas or new lines), lowercased."""
    return [word for word in (part.strip().lower() for part in re.split(r"[,\n]", raw or "")) if word]


def word_pattern(words: list[str]) -> re.Pattern[str] | None:
    """Matches any banned word or phrase as a whole word, in any case."""
    if not words:
        return None
    return re.compile(r"(?<!\w)(?:" + "|".join(re.escape(word) for word in sorted(words, key=len, reverse=True)) + r")(?!\w)", re.IGNORECASE)


def rule_action(settings: dict[str, Any], rule: str) -> str:
    action = str(settings.get(f"automod_{rule}_action", "off"))
    return action if action in ("delete", "warn", "mute") else "off"


def any_rule_enabled(settings: dict[str, Any]) -> bool:
    return any(rule_action(settings, rule) != "off" for rule in RULES)


def content_violation(content: str, mention_count: int, settings: dict[str, Any]) -> str | None:
    """The first content rule this message breaks: "invites", "words" or
    "mentions". Spam is about timing and is checked separately."""
    if rule_action(settings, "invites") != "off" and INVITE_PATTERN.search(content or ""):
        return "invites"
    if rule_action(settings, "words") != "off":
        pattern = word_pattern(banned_words(str(settings.get("automod_banned_words", ""))))
        if pattern is not None and pattern.search(content or ""):
            return "words"
    if rule_action(settings, "mentions") != "off" and mention_count >= int(settings.get("automod_mention_limit", 6) or 6):
        return "mentions"
    return None


class SpamTracker:
    """Remembers when each member last spoke, per server."""

    def __init__(self) -> None:
        self._seen: dict[tuple[int, int], deque[float]] = {}

    def hit(self, guild_id: int, user_id: int, limit: int, now: float | None = None) -> bool:
        """Record a message; True when it is the `limit`-th within the window.
        The count starts over after a hit, so one burst is one violation."""
        now = time.monotonic() if now is None else now
        stamps = self._seen.setdefault((guild_id, user_id), deque())
        while stamps and stamps[0] <= now - SPAM_WINDOW_SECONDS:
            stamps.popleft()
        stamps.append(now)
        if len(self._seen) > 5000:  # forget members who went quiet
            for key in [k for k, v in self._seen.items() if not v or v[-1] <= now - SPAM_WINDOW_SECONDS]:
                del self._seen[key]
        if len(stamps) >= limit:
            stamps.clear()
            return True
        return False
