from __future__ import annotations

import re
from datetime import timedelta

DURATION_PATTERN = re.compile(r"(\d+)\s*([smhdw])", re.IGNORECASE)
DURATION_UNIT_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}


def parse_duration(text: str) -> timedelta | None:
    """Parse a short duration string like '10m', '2h', '1d12h', '1w' into a timedelta."""
    matches = DURATION_PATTERN.findall(text.strip().lower())
    if not matches:
        return None

    total_seconds = sum(int(amount) * DURATION_UNIT_SECONDS[unit] for amount, unit in matches)
    if total_seconds <= 0:
        return None
    return timedelta(seconds=total_seconds)
