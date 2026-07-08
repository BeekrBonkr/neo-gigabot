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
