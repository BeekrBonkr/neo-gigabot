from __future__ import annotations

import asyncio
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import discord
from discord import app_commands

LOG_DIR_NAME = "logs"
LOG_FILENAME_FORMAT = "commands-%Y-%m-%d.log"
MAX_OPTION_VALUE_LENGTH = 200
DEFAULT_LOG_RETENTION_DAYS = 30

# One lock per log file path, so concurrent command completions across guilds
# can't interleave/corrupt a single day's file. Keyed by path rather than a
# single global lock so writes to *different* days never block each other.
_write_locks: dict[Path, asyncio.Lock] = {}


def _log_dir(storage_path: Path) -> Path:
    path = storage_path / LOG_DIR_NAME
    path.mkdir(parents=True, exist_ok=True)
    return path


def log_path_for_date(storage_path: Path, day: date) -> Path:
    return _log_dir(storage_path) / day.strftime(LOG_FILENAME_FORMAT)


def rotate_command_logs(storage_path: Path, *, max_age_days: int = DEFAULT_LOG_RETENTION_DAYS) -> int:
    """Delete daily command logs older than `max_age_days`. Returns how many
    files were removed. Files that don't match the commands-YYYY-MM-DD.log
    naming are left alone - this only manages what this module wrote."""
    cutoff = date.today() - timedelta(days=max_age_days)
    removed = 0
    log_dir = storage_path / LOG_DIR_NAME
    if not log_dir.is_dir():
        return 0

    for path in log_dir.glob("commands-*.log"):
        try:
            file_date = datetime.strptime(path.name, LOG_FILENAME_FORMAT).date()
        except ValueError:
            continue
        if file_date < cutoff:
            try:
                path.unlink()
                removed += 1
            except OSError:
                pass
    return removed


def _get_lock(path: Path) -> asyncio.Lock:
    lock = _write_locks.get(path)
    if lock is None:
        # A new path means a new day's file, so locks for previous days are
        # dead weight once nothing holds them - drop them instead of keeping
        # one lock per day for the life of the process.
        for stale_path in [p for p, l in _write_locks.items() if not l.locked()]:
            del _write_locks[stale_path]
        lock = asyncio.Lock()
        _write_locks[path] = lock
    return lock


def _format_option_value(value: Any) -> str:
    if isinstance(value, discord.Attachment):
        text = f"attachment:{value.filename}"
    elif isinstance(value, (discord.Member, discord.User)):
        text = f"{value} ({value.id})"
    elif isinstance(value, discord.abc.GuildChannel):
        text = f"#{value.name} ({value.id})"
    elif isinstance(value, discord.Role):
        text = f"@{value.name} ({value.id})"
    elif isinstance(value, app_commands.Choice):
        text = str(value.value)
    else:
        text = str(value)

    if len(text) > MAX_OPTION_VALUE_LENGTH:
        text = text[: MAX_OPTION_VALUE_LENGTH - 1].rstrip() + "…"
    return text


def _format_options(interaction: discord.Interaction) -> str:
    try:
        namespace = interaction.namespace
    except Exception:  # noqa: BLE001 - logging must never break command handling
        return ""

    items = [f"{key}={_format_option_value(value)}" for key, value in vars(namespace).items()]
    return ", ".join(items)


def format_entry(
    interaction: discord.Interaction,
    command: app_commands.Command | app_commands.ContextMenu | app_commands.Group | None,
    *,
    status: str = "OK",
    error: str | None = None,
) -> str:
    timestamp = discord.utils.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")

    if interaction.guild is not None:
        channel_name = getattr(interaction.channel, "name", None) or "unknown"
        location = (
            f"guild={interaction.guild.name} ({interaction.guild.id}) "
            f"channel=#{channel_name} ({interaction.channel_id})"
        )
    else:
        location = "guild=DM"

    name = getattr(command, "qualified_name", None) or "unknown"
    options = _format_options(interaction)

    parts = [
        timestamp,
        location,
        f"user={interaction.user} ({interaction.user.id})",
        f"command=/{name}",
    ]
    if options:
        parts.append(f"options=[{options}]")
    parts.append(f"status={status}")
    if error:
        parts.append(f"error={error}")

    return " | ".join(parts)


async def record_command(
    storage_path: Path,
    interaction: discord.Interaction,
    command: app_commands.Command | app_commands.ContextMenu | app_commands.Group | None,
    *,
    status: str = "OK",
    error: str | None = None,
) -> None:
    try:
        line = format_entry(interaction, command, status=status, error=error)
        path = log_path_for_date(storage_path, discord.utils.utcnow().date())
        async with _get_lock(path):
            with path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
    except OSError:
        pass  # a logging failure should never take down command handling


def parse_entry(line: str) -> dict[str, str] | None:
    """Parse one line written by format_entry back into its fields, or None
    for anything unrecognizable (partial writes, hand-edited files). The
    guild/channel pair shares one ' | ' segment, so it's split out here."""
    parts = line.rstrip("\n").split(" | ")
    if len(parts) < 3:
        return None

    entry: dict[str, str] = {"timestamp": parts[0]}
    for part in parts[1:]:
        if part.startswith("guild="):
            location = part[len("guild="):]
            guild, separator, channel = location.partition(" channel=")
            entry["guild"] = guild
            if separator:
                entry["channel"] = channel
            continue
        key, separator, value = part.partition("=")
        if separator:
            entry[key] = value

    return entry if "command" in entry else None


@dataclass
class CommandLogStats:
    """Aggregated view of one day's command log."""

    total: int = 0
    unparsed: int = 0
    by_command: Counter[str] = field(default_factory=Counter)
    by_status: Counter[str] = field(default_factory=Counter)
    by_user: Counter[str] = field(default_factory=Counter)
    by_guild: Counter[str] = field(default_factory=Counter)


def summarize_log(path: Path) -> CommandLogStats:
    stats = CommandLogStats()
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            entry = parse_entry(line)
            if entry is None:
                stats.unparsed += 1
                continue
            stats.total += 1
            stats.by_command[entry["command"]] += 1
            stats.by_status[entry.get("status", "UNKNOWN")] += 1
            stats.by_user[entry.get("user", "unknown")] += 1
            stats.by_guild[entry.get("guild", "unknown")] += 1
    return stats


def parse_day(value: str) -> date | None:
    normalized = value.strip().lower()
    today = discord.utils.utcnow().date()
    if normalized == "today":
        return today
    if normalized == "yesterday":
        return today - timedelta(days=1)
    try:
        return date.fromisoformat(normalized)
    except ValueError:
        return None
