from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any

import yaml

DEFAULT_GUILD_SETTINGS: dict[str, Any] = {
    "prefix": "!",
    "bot_channels": [],
    "blocked_commands": [],
    "logging": {
        "enabled": False,
        "channel_id": None,
        "ignored_channels": [],
        "ignored_users": [],
    },
    "welcome": {
        "enabled": False,
        "channel_id": None,
        "message": "Welcome to the server, {member_mention}!",
        "autoroles": [],
    },
}


def _guild_settings_path(storage_path: Path, guild_id: int) -> Path:
    return storage_path / "settings" / f"{guild_id}.yml"


def _default_settings_path(storage_path: Path) -> Path:
    return storage_path / "settings" / "default.yml"


def _merge_dict(base: dict[str, Any], overrides: dict[str, Any]) -> dict[str, Any]:
    merged = deepcopy(base)
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _merge_dict(merged[key], value)
        else:
            merged[key] = value
    return merged


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
    return _merge_dict(DEFAULT_GUILD_SETTINGS, loaded)


def get_guild_settings(storage_path: Path, guild_id: int) -> dict[str, Any]:
    defaults = get_default_settings(storage_path)
    path = _guild_settings_path(storage_path, guild_id)

    if not path.exists():
        save_guild_settings(storage_path, guild_id, defaults)
        return defaults

    with path.open("r", encoding="utf-8") as handle:
        loaded = yaml.safe_load(handle) or {}
    merged = _merge_dict(defaults, loaded)

    if merged != loaded:
        save_guild_settings(storage_path, guild_id, merged)

    return merged


def save_guild_settings(storage_path: Path, guild_id: int, data: dict[str, Any]) -> None:
    path = _guild_settings_path(storage_path, guild_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        yaml.safe_dump(data, handle, sort_keys=False)


def update_guild_settings(storage_path: Path, guild_id: int, patch: dict[str, Any]) -> dict[str, Any]:
    current = get_guild_settings(storage_path, guild_id)
    updated = _merge_dict(current, patch)
    save_guild_settings(storage_path, guild_id, updated)
    return updated
