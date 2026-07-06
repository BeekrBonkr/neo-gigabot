from __future__ import annotations

import asyncio
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import discord
from discord import app_commands

LOG_DIR_NAME = "logs"
LOG_FILENAME_FORMAT = "commands-%Y-%m-%d.log"
MAX_OPTION_VALUE_LENGTH = 200

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


def _get_lock(path: Path) -> asyncio.Lock:
    lock = _write_locks.get(path)
    if lock is None:
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
