from __future__ import annotations

from datetime import timedelta

import pytest

from utils.durations import parse_duration


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("10m", timedelta(minutes=10)),
        ("2h", timedelta(hours=2)),
        ("1d", timedelta(days=1)),
        ("1w", timedelta(weeks=1)),
        ("45s", timedelta(seconds=45)),
        ("1d12h", timedelta(days=1, hours=12)),
        ("1h30m15s", timedelta(hours=1, minutes=30, seconds=15)),
        ("  2H  ", timedelta(hours=2)),
        ("1d 12h", timedelta(days=1, hours=12)),
    ],
)
def test_parse_valid_durations(text: str, expected: timedelta) -> None:
    assert parse_duration(text) == expected


@pytest.mark.parametrize("text", ["", "banana", "10", "m", "-5m", "0s", "0m0h"])
def test_parse_invalid_durations(text: str) -> None:
    assert parse_duration(text) is None


def test_units_accumulate_even_when_repeated() -> None:
    assert parse_duration("1h1h") == timedelta(hours=2)


def test_slowmode_delays_accept_off_seconds_and_durations_up_to_six_hours():
    from cogs.moderation import format_seconds, parse_slowmode

    assert [parse_slowmode(v) for v in ("off", "0", "30", "2m", "1h30m", "6h")] == [0, 0, 30, 120, 5400, 21600]
    assert [parse_slowmode(v) for v in ("7h", "21601", "soon", "", "-5")] == [None, None, None, None, None]
    assert [format_seconds(v) for v in (30, 120, 5400, 3661)] == ["30s", "2m", "1h 30m", "1h 1m 1s"]
