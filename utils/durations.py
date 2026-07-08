from __future__ import annotations

import re
from datetime import timedelta

DURATION_PATTERN = re.compile(r"(\d+)\s*([smhdw])", re.IGNORECASE)
# The whole input must be duration tokens - otherwise findall() would happily
# pull "5m" out of garbage like "-5m" or "ban 5m" and return a bogus positive
# duration instead of rejecting the input.
FULL_DURATION_PATTERN = re.compile(r"(?:\d+\s*[smhdw]\s*)+", re.IGNORECASE)
DURATION_UNIT_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}


def parse_duration(text: str) -> timedelta | None:
    """Parse a short duration string like '10m', '2h', '1d12h', '1w' into a timedelta."""
    normalized = text.strip().lower()
    if not FULL_DURATION_PATTERN.fullmatch(normalized):
        return None
    matches = DURATION_PATTERN.findall(normalized)
    if not matches:
        return None

    total_seconds = sum(int(amount) * DURATION_UNIT_SECONDS[unit] for amount, unit in matches)
    if total_seconds <= 0:
        return None
    return timedelta(seconds=total_seconds)
