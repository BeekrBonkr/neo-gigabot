from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import threading
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

LOGGER = logging.getLogger(__name__)
SCHEMA_VERSION = 1
ALLOWED_PREFIX_CHARS = "!$%&*.<>"
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


def _default_settings_path(storage_path: Path) -> Path:
    return storage_path / "settings" / "default.yml"


def _legacy_yaml_dir(storage_path: Path) -> Path:
    return storage_path / "settings"


def _database_path(storage_path: Path) -> Path:
    return storage_path / "settings.db"


def sync_with_default(server_data: dict[str, Any], default_data: dict[str, Any]) -> dict[str, Any]:
    """Fill in missing keys without deleting unknown keys.

    Preserving unknown keys makes legacy migration safer while the recode is
    still absorbing old Gigabot features.
    """
    synced = deepcopy(server_data or {})
    for key, value in default_data.items():
        if key not in synced:
            synced[key] = deepcopy(value)
    return synced


def apply_patch(server_data: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    updated = deepcopy(server_data)
    updated.update(patch)
    return updated


class SettingsManager:
    def __init__(self, flush_delay: float = 3.0) -> None:
        self.flush_delay = flush_delay
        self._storage_path: Path | None = None
        self._db_path: Path | None = None
        self._defaults: dict[str, Any] = deepcopy(DEFAULT_GUILD_SETTINGS)
        self._cache: dict[int, dict[str, Any]] = {}
        self._dirty: set[int] = set()
        self._flush_handle: asyncio.TimerHandle | None = None
        self._lock = threading.RLock()
        self._initialized = False

    def initialize(self, storage_path: Path) -> None:
        with self._lock:
            if self._initialized and self._storage_path == storage_path:
                return

            self._storage_path = storage_path
            self._db_path = _database_path(storage_path)
            storage_path.mkdir(parents=True, exist_ok=True)
            _legacy_yaml_dir(storage_path).mkdir(parents=True, exist_ok=True)
            self._defaults = self._load_defaults_from_yaml(storage_path)
            self._ensure_database()
            self._initialized = True

        self.import_legacy_yaml_configs(overwrite_existing=False)

    def _require_initialized(self) -> tuple[Path, Path]:
        if self._storage_path is None or self._db_path is None:
            raise RuntimeError("SettingsManager has not been initialized.")
        return self._storage_path, self._db_path

    def _connect(self) -> sqlite3.Connection:
        _, db_path = self._require_initialized()
        connection = sqlite3.connect(db_path)
        connection.row_factory = sqlite3.Row
        return connection

    def _ensure_database(self) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS guild_settings (
                    guild_id INTEGER PRIMARY KEY,
                    schema_version INTEGER NOT NULL,
                    data_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            connection.commit()

    def _load_defaults_from_yaml(self, storage_path: Path) -> dict[str, Any]:
        path = _default_settings_path(storage_path)
        path.parent.mkdir(parents=True, exist_ok=True)

        loaded: dict[str, Any] = {}
        if path.exists():
            with path.open("r", encoding="utf-8") as handle:
                loaded = yaml.safe_load(handle) or {}

        defaults = sync_with_default(loaded, DEFAULT_GUILD_SETTINGS)

        if defaults != loaded:
            with path.open("w", encoding="utf-8") as handle:
                yaml.safe_dump(defaults, handle, sort_keys=False)

        return defaults

    def get_default_settings(self) -> dict[str, Any]:
        return deepcopy(self._defaults)

    def guild_exists(self, guild_id: int | str) -> bool:
        guild_id_int = int(guild_id)
        with self._lock:
            if guild_id_int in self._cache:
                return True
        with self._connect() as connection:
            row = connection.execute(
                "SELECT 1 FROM guild_settings WHERE guild_id = ?",
                (guild_id_int,),
            ).fetchone()
        return row is not None

    def create_guild_settings(self, guild_id: int | str) -> dict[str, Any]:
        guild_id_int = int(guild_id)
        settings = self.get_guild_settings(guild_id_int)
        self.mark_dirty(guild_id_int)
        return settings

    def delete_guild_settings(self, guild_id: int | str) -> None:
        guild_id_int = int(guild_id)
        with self._lock:
            self._cache.pop(guild_id_int, None)
            self._dirty.discard(guild_id_int)
        with self._connect() as connection:
            connection.execute("DELETE FROM guild_settings WHERE guild_id = ?", (guild_id_int,))
            connection.commit()

    def _load_from_db(self, guild_id: int) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT data_json, schema_version FROM guild_settings WHERE guild_id = ?",
                (guild_id,),
            ).fetchone()
        if row is None:
            return None

        try:
            data = json.loads(row["data_json"])
        except json.JSONDecodeError:
            LOGGER.exception("Failed to decode settings JSON for guild %s", guild_id)
            data = {}

        if not isinstance(data, dict):
            data = {}

        return self._normalize_loaded_settings(data)

    def _normalize_loaded_settings(self, data: dict[str, Any]) -> dict[str, Any]:
        normalized = sync_with_default(data, self._defaults)
        prefix = str(normalized.get("prefix", self._defaults["prefix"]))
        if not prefix or not all(character in ALLOWED_PREFIX_CHARS for character in prefix):
            normalized["prefix"] = self._defaults["prefix"]
        return normalized

    def get_guild_settings(self, guild_id: int | str) -> dict[str, Any]:
        guild_id_int = int(guild_id)
        with self._lock:
            cached = self._cache.get(guild_id_int)
            if cached is not None:
                return deepcopy(cached)

        loaded = self._load_from_db(guild_id_int)
        if loaded is None:
            loaded = self._normalize_loaded_settings({})
            with self._lock:
                self._cache[guild_id_int] = deepcopy(loaded)
            self.mark_dirty(guild_id_int)
            return deepcopy(loaded)

        with self._lock:
            self._cache[guild_id_int] = deepcopy(loaded)
        return deepcopy(loaded)

    def save_guild_settings(self, guild_id: int | str, data: dict[str, Any]) -> None:
        guild_id_int = int(guild_id)
        normalized = self._normalize_loaded_settings(data)
        with self._lock:
            self._cache[guild_id_int] = deepcopy(normalized)
        self.mark_dirty(guild_id_int)

    def update_guild_settings(self, guild_id: int | str, patch: dict[str, Any]) -> dict[str, Any]:
        current = self.get_guild_settings(guild_id)
        updated = apply_patch(current, patch)
        self.save_guild_settings(guild_id, updated)
        return updated

    def mark_dirty(self, guild_id: int | str) -> None:
        guild_id_int = int(guild_id)
        with self._lock:
            self._dirty.add(guild_id_int)
        self._schedule_flush()

    def _schedule_flush(self) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return

        if self._flush_handle is not None and not self._flush_handle.cancelled():
            self._flush_handle.cancel()

        self._flush_handle = loop.call_later(
            self.flush_delay,
            lambda: asyncio.create_task(asyncio.to_thread(self.flush_dirty)),
        )

    def flush_dirty(self) -> None:
        with self._lock:
            dirty_ids = list(self._dirty)
            if not dirty_ids:
                return
            payload = {guild_id: deepcopy(self._cache[guild_id]) for guild_id in dirty_ids if guild_id in self._cache}

        timestamp = datetime.now(UTC).isoformat()
        with self._connect() as connection:
            connection.executemany(
                """
                INSERT INTO guild_settings (guild_id, schema_version, data_json, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(guild_id) DO UPDATE SET
                    schema_version = excluded.schema_version,
                    data_json = excluded.data_json,
                    updated_at = excluded.updated_at
                """,
                [
                    (
                        guild_id,
                        SCHEMA_VERSION,
                        json.dumps(data, ensure_ascii=False, sort_keys=True),
                        timestamp,
                    )
                    for guild_id, data in payload.items()
                ],
            )
            connection.commit()

        with self._lock:
            self._dirty.difference_update(payload.keys())

    async def flush_all_async(self) -> None:
        await asyncio.to_thread(self.flush_dirty)

    def import_legacy_yaml_configs(self, overwrite_existing: bool = False) -> int:
        storage_path, _ = self._require_initialized()
        settings_dir = _legacy_yaml_dir(storage_path)
        imported = 0

        for file_path in settings_dir.glob("*.yml"):
            if file_path.name == "default.yml":
                continue
            try:
                guild_id = int(file_path.stem)
            except ValueError:
                continue

            if not overwrite_existing and self.guild_exists(guild_id):
                continue

            try:
                with file_path.open("r", encoding="utf-8") as handle:
                    loaded = yaml.safe_load(handle) or {}
            except OSError:
                LOGGER.exception("Failed to read legacy settings file: %s", file_path)
                continue

            if not isinstance(loaded, dict):
                LOGGER.warning("Skipping legacy settings file with invalid data: %s", file_path)
                continue

            self.save_guild_settings(guild_id, loaded)
            imported += 1

        if imported:
            self.flush_dirty()
            LOGGER.info("Imported %s legacy YAML settings file(s) into SQLite", imported)
        return imported


_MANAGER = SettingsManager()


# ===== public compatibility helpers =====
def initialize_settings(storage_path: Path) -> None:
    _MANAGER.initialize(storage_path)


def flush_settings() -> None:
    _MANAGER.flush_dirty()


async def flush_settings_async() -> None:
    await _MANAGER.flush_all_async()


def ensure_default_settings_file(storage_path: Path) -> None:
    initialize_settings(storage_path)


def get_default_settings(storage_path: Path) -> dict[str, Any]:
    initialize_settings(storage_path)
    return _MANAGER.get_default_settings()


def guild_settings_exists(storage_path: Path, guild_id: int | str) -> bool:
    initialize_settings(storage_path)
    return _MANAGER.guild_exists(guild_id)


def create_guild_settings(storage_path: Path, guild_id: int | str) -> dict[str, Any]:
    initialize_settings(storage_path)
    return _MANAGER.create_guild_settings(guild_id)


def delete_guild_settings(storage_path: Path, guild_id: int | str) -> None:
    initialize_settings(storage_path)
    _MANAGER.delete_guild_settings(guild_id)


def get_guild_settings(storage_path: Path, guild_id: int | str) -> dict[str, Any]:
    initialize_settings(storage_path)
    return _MANAGER.get_guild_settings(guild_id)


def save_guild_settings(storage_path: Path, guild_id: int | str, data: dict[str, Any]) -> None:
    initialize_settings(storage_path)
    _MANAGER.save_guild_settings(guild_id, data)


def update_guild_settings(storage_path: Path, guild_id: int | str, patch: dict[str, Any]) -> dict[str, Any]:
    initialize_settings(storage_path)
    return _MANAGER.update_guild_settings(guild_id, patch)


def import_legacy_yaml_configs(storage_path: Path, overwrite_existing: bool = False) -> int:
    initialize_settings(storage_path)
    return _MANAGER.import_legacy_yaml_configs(overwrite_existing=overwrite_existing)


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
