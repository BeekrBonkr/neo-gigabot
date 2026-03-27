from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any

import yaml

# Keep this structure intentionally close to the legacy project so we can
# migrate commands without changing the YAML contract yet.
DEFAULT_GUILD_SETTINGS: dict[str, Any] = {
    "prefix": "!",
    "suggestion_channel_ids": [],
    "bot_channels": [],
    "blocked_commands": [],
    "listen_to_bots": False,
    "logging_enabled": False,
    "logging_channel_id": "",
    "ignored_log_channels": [],
    "ignored_users": [],
    "join_message": "Welcome {mention} to {server}!",
    "leave_message": "{mention} has left the server.",
    "welcome_channel_ids": [],
    "autoroles": [],
}


ALLOWED_PREFIX_CHARS = "!$%&*.<>"


def _guild_settings_path(storage_path: Path, guild_id: int | str) -> Path:
    return storage_path / "settings" / f"{guild_id}.yml"


def _default_settings_path(storage_path: Path) -> Path:
    return storage_path / "settings" / "default.yml"


# legacy behavior: only add missing keys / remove extra keys, do not deep-merge
# nested dicts because the legacy file format is flat anyway.
def sync_with_default(server_data: dict[str, Any], default_data: dict[str, Any]) -> dict[str, Any]:
    synced = deepcopy(server_data or {})

    for key, value in default_data.items():
        if key not in synced:
            synced[key] = deepcopy(value)

    keys_to_remove = [key for key in synced.keys() if key not in default_data]
    for key in keys_to_remove:
        del synced[key]

    return synced


# small helper for targeted updates without changing the rest of the file
# shape.
def apply_patch(server_data: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    updated = deepcopy(server_data)
    updated.update(patch)
    return updated


# ===== default settings helpers =====
def ensure_default_settings_file(storage_path: Path) -> None:
    path = _default_settings_path(storage_path)
    if path.exists():
        return

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        yaml.safe_dump(DEFAULT_GUILD_SETTINGS, handle, sort_keys=False)



def get_default_settings(storage_path: Path) -> dict[str, Any]:
    ensure_default_settings_file(storage_path)
    with _default_settings_path(storage_path).open("r", encoding="utf-8") as handle:
        loaded = yaml.safe_load(handle) or {}
    return sync_with_default(loaded, DEFAULT_GUILD_SETTINGS)


# ===== guild settings helpers =====
def guild_settings_exists(storage_path: Path, guild_id: int | str) -> bool:
    return _guild_settings_path(storage_path, guild_id).exists()



def create_guild_settings(storage_path: Path, guild_id: int | str) -> dict[str, Any]:
    defaults = get_default_settings(storage_path)
    save_guild_settings(storage_path, guild_id, defaults)
    return defaults



def delete_guild_settings(storage_path: Path, guild_id: int | str) -> None:
    path = _guild_settings_path(storage_path, guild_id)
    if path.exists():
        path.unlink()



def get_guild_settings(storage_path: Path, guild_id: int | str) -> dict[str, Any]:
    defaults = get_default_settings(storage_path)
    path = _guild_settings_path(storage_path, guild_id)

    if not path.exists():
        save_guild_settings(storage_path, guild_id, defaults)
        return defaults

    with path.open("r", encoding="utf-8") as handle:
        loaded = yaml.safe_load(handle) or {}

    synced = sync_with_default(loaded, defaults)
    if synced != loaded:
        save_guild_settings(storage_path, guild_id, synced)

    return synced



def save_guild_settings(storage_path: Path, guild_id: int | str, data: dict[str, Any]) -> None:
    path = _guild_settings_path(storage_path, guild_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        yaml.safe_dump(data, handle, sort_keys=False)



def update_guild_settings(storage_path: Path, guild_id: int | str, patch: dict[str, Any]) -> dict[str, Any]:
    current = get_guild_settings(storage_path, guild_id)
    updated = apply_patch(current, patch)
    save_guild_settings(storage_path, guild_id, updated)
    return updated


# ===== convenience helpers used by other cogs =====
def get_guild_prefix(storage_path: Path, guild_id: int | str, fallback: str = "!") -> str:
    settings = get_guild_settings(storage_path, guild_id)
    return settings.get("prefix", fallback)



def command_is_blocked(storage_path: Path, guild_id: int | str, command_name: str) -> bool:
    settings = get_guild_settings(storage_path, guild_id)
    blocked = settings.get("blocked_commands", []) or []
    return command_name.lower() in {str(name).lower() for name in blocked}



def is_bot_channel(storage_path: Path, guild_id: int | str, channel_id: int) -> bool:
    settings = get_guild_settings(storage_path, guild_id)
    bot_channels = settings.get("bot_channels", []) or []
    return channel_id in bot_channels
