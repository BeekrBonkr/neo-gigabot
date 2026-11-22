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


def test_a_line_says_whether_the_command_ran_in_a_server_with_the_bot() -> None:
    from types import SimpleNamespace as NS

    from utils.command_log import format_entry, parse_entry, place_label

    user = type("U", (), {"id": 42, "__str__": lambda self: "robin"})()
    command = NS(qualified_name="filter invert")

    def run(guild=None, home=True, private=False):
        return format_entry(NS(guild=guild, channel=NS(name="general"), channel_id=7, user=user, namespace=NS(), data={},
                               is_guild_integration=lambda: home, context=NS(private_channel=private)), command)

    home = run(NS(name="My Server", id=123))
    outside = run(NS(name="", id=555), home=False)          # the bot is told this server's ID and nothing else
    named = run(NS(name="Spoofed Name", id=555), home=False)  # and a name is never taken from a server it is not in
    private, dm = run(private=True), run()
    assert "guild=My Server (123) channel=#general (7)" in home
    assert "guild= (555) channel=#general (7)" in outside and "guild= (555)" in named
    assert "| guild=PRIVATE |" in private and "| guild=DM |" in dm
    labels = [place_label(parse_entry(line)["guild"]) for line in (home, outside, private, dm)]
    assert labels == ["My Server (123)", "Personal app (servers without the bot)", "Personal app (private chats)", "DM"]


def test_the_days_summary_groups_personal_app_use(storage: Path) -> None:
    from utils.command_log import summarize_log

    path = storage / "day.log"
    path.write_text(
        "2026-07-08 12:00:00 UTC | guild= (555) channel=#unknown (7) | user=a (1) | command=/filter invert | status=OK\n"
        "2026-07-08 12:00:01 UTC | guild= (556) channel=#unknown (8) | user=a (1) | command=/filter invert | status=OK\n"
        "2026-07-08 12:00:02 UTC | guild=PRIVATE | user=a (1) | command=/roll | status=OK\n"
        "2026-07-08 12:00:03 UTC | guild=DM | user=a (1) | command=/roll | status=OK\n",
        encoding="utf-8",
    )
    assert dict(summarize_log(path).by_guild) == {"Personal app (servers without the bot)": 2, "Personal app (private chats)": 1, "DM": 1}
