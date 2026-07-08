from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

from utils.command_log import LOG_FILENAME_FORMAT, parse_entry, rotate_command_logs, summarize_log


def _make_log(storage: Path, day: date) -> Path:
    log_dir = storage / "logs"
    log_dir.mkdir(exist_ok=True)
    path = log_dir / day.strftime(LOG_FILENAME_FORMAT)
    path.write_text("log line\n", encoding="utf-8")
    return path


def test_rotate_deletes_only_old_logs(storage: Path) -> None:
    today = date.today()
    fresh = _make_log(storage, today)
    borderline = _make_log(storage, today - timedelta(days=29))
    old = _make_log(storage, today - timedelta(days=31))
    ancient = _make_log(storage, today - timedelta(days=365))

    assert rotate_command_logs(storage, max_age_days=30) == 2
    assert fresh.exists()
    assert borderline.exists()
    assert not old.exists()
    assert not ancient.exists()


def test_rotate_ignores_unrelated_files(storage: Path) -> None:
    log_dir = storage / "logs"
    log_dir.mkdir()
    unrelated = log_dir / "commands-notadate.log"
    unrelated.write_text("keep me\n", encoding="utf-8")
    other = log_dir / "somethingelse.txt"
    other.write_text("keep me too\n", encoding="utf-8")

    assert rotate_command_logs(storage, max_age_days=0) == 0
    assert unrelated.exists()
    assert other.exists()


def test_rotate_without_log_dir_is_noop(storage: Path) -> None:
    assert rotate_command_logs(storage) == 0


GUILD_LINE = (
    "2026-07-08 12:00:00 UTC | guild=My Server (123) channel=#general (456) | "
    "user=alice (1) | command=/playlist add | options=[name=mix] | status=OK"
)
DM_LINE = "2026-07-08 12:01:00 UTC | guild=DM | user=bob (2) | command=/owner system | status=OK"
ERROR_LINE = (
    "2026-07-08 12:02:00 UTC | guild=My Server (123) channel=#general (456) | "
    "user=alice (1) | command=/meme | status=ERROR | error=ValueError"
)


def test_parse_entry_round_trips_fields() -> None:
    entry = parse_entry(GUILD_LINE)
    assert entry is not None
    assert entry["guild"] == "My Server (123)"
    assert entry["channel"] == "#general (456)"
    assert entry["user"] == "alice (1)"
    assert entry["command"] == "/playlist add"
    assert entry["status"] == "OK"

    dm_entry = parse_entry(DM_LINE)
    assert dm_entry is not None
    assert dm_entry["guild"] == "DM"
    assert "channel" not in dm_entry

    assert parse_entry("garbage line") is None
    assert parse_entry("a | b | c | no command here") is None


def test_summarize_log_counts(storage: Path) -> None:
    path = storage / "logs" / "commands-2026-07-08.log"
    path.parent.mkdir(exist_ok=True)
    path.write_text("\n".join([GUILD_LINE, DM_LINE, ERROR_LINE, GUILD_LINE, "corrupt"]) + "\n", encoding="utf-8")

    stats = summarize_log(path)
    assert stats.total == 4
    assert stats.unparsed == 1
    assert stats.by_command["/playlist add"] == 2
    assert stats.by_status == {"OK": 3, "ERROR": 1}
    assert stats.by_user["alice (1)"] == 3
    assert stats.by_guild == {"My Server (123)": 3, "DM": 1}
