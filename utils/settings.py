from __future__ import annotations

import json
import sqlite3
from copy import deepcopy
from pathlib import Path
from typing import Any

import yaml

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
    "music_dj_enabled": False,
    "music_dj_role_name": "dj",
    "music_default_volume": 50,
}

ALLOWED_PREFIX_CHARS = "!$%&*.<>"


# ==============================
# paths
# ==============================
def _default_settings_path(storage_path: Path) -> Path:
    return storage_path / "settings" / "default.yml"


def _settings_db_path(storage_path: Path) -> Path:
    return storage_path / "settings.db"


# ==============================
# default schema helpers
# ==============================
def _merge_missing_defaults(loaded: Any, built_in: Any) -> Any:
    if isinstance(loaded, dict) and isinstance(built_in, dict):
        merged: dict[str, Any] = deepcopy(loaded)
        for key, value in built_in.items():
            if key not in merged:
                merged[key] = deepcopy(value)
            else:
                merged[key] = _merge_missing_defaults(merged[key], value)
        return merged
    return deepcopy(loaded)


# strict schema sync:
# - add missing keys from defaults
# - remove keys not present in defaults
# - keep existing values for keys that still exist
# - recurse into nested dicts

def sync_with_default(server_data: dict[str, Any], default_data: dict[str, Any]) -> dict[str, Any]:
    synced: dict[str, Any] = {}
    source = server_data or {}

    for key, default_value in default_data.items():
        if key not in source:
            synced[key] = deepcopy(default_value)
            continue

        current_value = source[key]
        if isinstance(default_value, dict) and isinstance(current_value, dict):
            synced[key] = sync_with_default(current_value, default_value)
        else:
            synced[key] = deepcopy(current_value)

    return synced


def apply_patch(server_data: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    updated = deepcopy(server_data)
    updated.update(patch)
    return updated


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

    if not isinstance(loaded, dict):
        loaded = {}

    return _merge_missing_defaults(loaded, DEFAULT_GUILD_SETTINGS)


# ==============================
# sqlite helpers
# ==============================
def _connect(storage_path: Path) -> sqlite3.Connection:
    db_path = _settings_db_path(storage_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    return connection


CREATE_GUILD_SETTINGS_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS guild_settings (
    guild_id INTEGER PRIMARY KEY,
    data TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
)
"""


def ensure_settings_database(storage_path: Path) -> None:
    with _connect(storage_path) as connection:
        connection.execute(CREATE_GUILD_SETTINGS_TABLE_SQL)
        connection.commit()


def _serialize(data: dict[str, Any]) -> str:
    return json.dumps(data, sort_keys=True, separators=(",", ":"))


def _deserialize(payload: str) -> dict[str, Any]:
    loaded = json.loads(payload)
    if isinstance(loaded, dict):
        return loaded
    return {}


def _fetch_raw_guild_settings(storage_path: Path, guild_id: int | str) -> dict[str, Any] | None:
    ensure_settings_database(storage_path)
    with _connect(storage_path) as connection:
        row = connection.execute(
            "SELECT data FROM guild_settings WHERE guild_id = ?",
            (int(guild_id),),
        ).fetchone()

    if row is None:
        return None

    return _deserialize(row["data"])


def _upsert_guild_settings(storage_path: Path, guild_id: int | str, data: dict[str, Any]) -> None:
    ensure_settings_database(storage_path)
    payload = _serialize(data)
    with _connect(storage_path) as connection:
        connection.execute(
            """
            INSERT INTO guild_settings (guild_id, data, updated_at)
            VALUES (?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(guild_id) DO UPDATE SET
                data = excluded.data,
                updated_at = CURRENT_TIMESTAMP
            """,
            (int(guild_id), payload),
        )
        connection.commit()


# ==============================
# guild settings helpers
# ==============================
def guild_settings_exists(storage_path: Path, guild_id: int | str) -> bool:
    return _fetch_raw_guild_settings(storage_path, guild_id) is not None


def create_guild_settings(storage_path: Path, guild_id: int | str) -> dict[str, Any]:
    defaults = get_default_settings(storage_path)
    _upsert_guild_settings(storage_path, guild_id, defaults)
    return deepcopy(defaults)


def delete_guild_settings(storage_path: Path, guild_id: int | str) -> None:
    ensure_settings_database(storage_path)
    with _connect(storage_path) as connection:
        connection.execute("DELETE FROM guild_settings WHERE guild_id = ?", (int(guild_id),))
        connection.commit()


def get_guild_settings(storage_path: Path, guild_id: int | str) -> dict[str, Any]:
    defaults = get_default_settings(storage_path)
    loaded = _fetch_raw_guild_settings(storage_path, guild_id)

    if loaded is None:
        _upsert_guild_settings(storage_path, guild_id, defaults)
        return deepcopy(defaults)

    synced = sync_with_default(loaded, defaults)
    if synced != loaded:
        _upsert_guild_settings(storage_path, guild_id, synced)

    return synced


def save_guild_settings(storage_path: Path, guild_id: int | str, data: dict[str, Any]) -> None:
    defaults = get_default_settings(storage_path)
    synced = sync_with_default(data, defaults)
    _upsert_guild_settings(storage_path, guild_id, synced)


def update_guild_settings(
    storage_path: Path,
    guild_id: int | str,
    patch: dict[str, Any],
) -> dict[str, Any]:
    current = get_guild_settings(storage_path, guild_id)
    updated = apply_patch(current, patch)
    save_guild_settings(storage_path, guild_id, updated)
    return get_guild_settings(storage_path, guild_id)


def sync_all_guild_settings(storage_path: Path) -> int:
    defaults = get_default_settings(storage_path)
    ensure_settings_database(storage_path)

    with _connect(storage_path) as connection:
        rows = connection.execute("SELECT guild_id, data FROM guild_settings").fetchall()

    updated_count = 0
    for row in rows:
        guild_id = int(row["guild_id"])
        loaded = _deserialize(row["data"])
        synced = sync_with_default(loaded, defaults)
        if synced != loaded:
            _upsert_guild_settings(storage_path, guild_id, synced)
            updated_count += 1

    return updated_count


# ==============================
# convenience helpers used by other cogs
# ==============================
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
