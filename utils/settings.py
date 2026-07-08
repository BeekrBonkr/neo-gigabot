from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any

import yaml

from utils.db import connect as _db_connect

DEFAULT_GUILD_SETTINGS: dict[str, Any] = {
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
    "music_djadmin_role_id": "",
    "music_default_volume": 50,
    "mod_log_enabled": False,
    "mod_log_channel_id": "",
    "warn_threshold": 0,
    "warn_action": "none",
    "warn_mute_duration": "1h",
    "warn_window_days": 0,
    "sticky_roles": False,
    "dm_on_mod_action": True,
    "mod_log_events": ["warn", "mute", "kick", "ban"],
    "starboard_channel_id": "",
    "starboard_threshold": 3,
    "starboard_emoji": "⭐",
    "starboard_allow_selfstar": True,
    "starboard_ignore_nsfw": False,
    "welcome_delete_after": 0,
    "leave_channel_ids": [],
    "embed_color": "",
    "music_max_queue_length": 0,
    "music_max_track_minutes": 0,
}

WARN_ACTION_CHOICES = ("none", "mute", "kick", "ban")
MOD_LOG_EVENT_KINDS = ("warn", "mute", "kick", "ban")


# ==============================
# in-memory caches
# ==============================
# Guild settings are read on every gated command *and* on every logged event
# (message edits/deletes, joins, voice updates, ...), so hitting SQLite and
# re-parsing default.yml per read is the bot's hottest path by far. Both caches
# are safe without locking because everything runs on one event loop and the
# mutating helpers below keep them in sync with every write. Copies are handed
# out (and stored) so callers that mutate the returned dict before saving can't
# corrupt the cached version.
#
# Trade-off: hand-editing default.yml or the DB while the bot is running won't
# be picked up until restart.
_DEFAULTS_CACHE: dict[str, dict[str, Any]] = {}
_GUILD_CACHE: dict[tuple[str, int], dict[str, Any]] = {}


def clear_settings_caches() -> None:
    """Drop both caches. Used by tests and available for a manual reload."""
    _DEFAULTS_CACHE.clear()
    _GUILD_CACHE.clear()


# ==============================
# paths
# ==============================
def _default_settings_path(storage_path: Path) -> Path:
    return storage_path / "settings" / "default.yml"


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
    cache_key = str(storage_path)
    cached = _DEFAULTS_CACHE.get(cache_key)
    if cached is not None:
        return deepcopy(cached)

    ensure_default_settings_file(storage_path)
    with _default_settings_path(storage_path).open("r", encoding="utf-8") as handle:
        loaded = yaml.safe_load(handle) or {}

    if not isinstance(loaded, dict):
        loaded = {}

    merged = _merge_missing_defaults(loaded, DEFAULT_GUILD_SETTINGS)
    _DEFAULTS_CACHE[cache_key] = deepcopy(merged)
    return merged


# ==============================
# sqlite helpers
# ==============================
def _connect(storage_path: Path):
    return _db_connect(storage_path)


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
    _GUILD_CACHE[(str(storage_path), int(guild_id))] = deepcopy(data)


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
    _GUILD_CACHE.pop((str(storage_path), int(guild_id)), None)


def get_guild_settings(storage_path: Path, guild_id: int | str) -> dict[str, Any]:
    cached = _GUILD_CACHE.get((str(storage_path), int(guild_id)))
    if cached is not None:
        return deepcopy(cached)

    defaults = get_default_settings(storage_path)
    loaded = _fetch_raw_guild_settings(storage_path, guild_id)

    if loaded is None:
        _upsert_guild_settings(storage_path, guild_id, defaults)
        return deepcopy(defaults)

    synced = sync_with_default(loaded, defaults)
    if synced != loaded:
        _upsert_guild_settings(storage_path, guild_id, synced)
    else:
        _GUILD_CACHE[(str(storage_path), int(guild_id))] = deepcopy(synced)

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


def migrate_blocked_command_names(storage_path: Path, leaf_to_qualified: dict[str, list[str]]) -> int:
    """One-time upgrade of blocked_commands entries from leaf names to
    qualified names ("add" -> "playlist add" / "reactionrole add" / ...).

    Each stored leaf name expands to *every* qualified name sharing that leaf,
    which preserves the old matching behavior exactly (leaf-name matching
    blocked all of them); admins can then unblock the unintended ones
    individually from the dashboard. Entries containing a space are already
    qualified and pass through; unknown names are kept as-is. Returns the
    number of guild rows rewritten."""
    ensure_settings_database(storage_path)
    with _connect(storage_path) as connection:
        rows = connection.execute("SELECT guild_id, data FROM guild_settings").fetchall()

    migrated = 0
    for row in rows:
        data = _deserialize(row["data"])
        blocked = data.get("blocked_commands")
        if not blocked:
            continue

        expanded: list[str] = []
        for name in blocked:
            name = str(name).lower()
            if " " in name:
                expanded.append(name)
            else:
                expanded.extend(leaf_to_qualified.get(name, [name]))

        new_blocked = sorted(set(expanded))
        if new_blocked != blocked:
            data["blocked_commands"] = new_blocked
            _upsert_guild_settings(storage_path, int(row["guild_id"]), data)
            migrated += 1

    return migrated


# ==============================
# convenience helpers used by other cogs
# ==============================
def command_is_blocked(storage_path: Path, guild_id: int | str, command_name: str) -> bool:
    settings = get_guild_settings(storage_path, guild_id)
    blocked = settings.get("blocked_commands", []) or []
    return command_name.lower() in {str(name).lower() for name in blocked}


def is_bot_channel(storage_path: Path, guild_id: int | str, channel_id: int) -> bool:
    settings = get_guild_settings(storage_path, guild_id)
    bot_channels = settings.get("bot_channels", []) or []

    if not bot_channels:
        return True

    normalized_bot_channels = {int(value) for value in bot_channels}
    return int(channel_id) in normalized_bot_channels


# ==============================
# activity logging helpers
# ==============================
def is_logging_enabled(storage_path: Path, guild_id: int | str) -> bool:
    settings = get_guild_settings(storage_path, guild_id)
    return bool(settings.get("logging_enabled", False)) and bool(settings.get("logging_channel_id"))


def is_log_channel_ignored(storage_path: Path, guild_id: int | str, channel_id: int) -> bool:
    settings = get_guild_settings(storage_path, guild_id)
    ignored = settings.get("ignored_log_channels", []) or []
    return int(channel_id) in {int(value) for value in ignored}


def is_log_user_ignored(storage_path: Path, guild_id: int | str, user_id: int) -> bool:
    settings = get_guild_settings(storage_path, guild_id)
    ignored = settings.get("ignored_users", []) or []
    return int(user_id) in {int(value) for value in ignored}


def get_logging_channel_id(storage_path: Path, guild_id: int | str) -> int | None:
    settings = get_guild_settings(storage_path, guild_id)
    channel_id = settings.get("logging_channel_id")
    if not channel_id:
        return None
    try:
        return int(channel_id)
    except (TypeError, ValueError):
        return None


# ==============================
# starboard helpers
# ==============================
def get_starboard_channel_id(storage_path: Path, guild_id: int | str) -> int | None:
    settings = get_guild_settings(storage_path, guild_id)
    channel_id = settings.get("starboard_channel_id")
    if not channel_id:
        return None
    try:
        return int(channel_id)
    except (TypeError, ValueError):
        return None


def get_starboard_threshold(storage_path: Path, guild_id: int | str) -> int:
    settings = get_guild_settings(storage_path, guild_id)
    try:
        return max(1, int(settings.get("starboard_threshold", 3) or 3))
    except (TypeError, ValueError):
        return 3


def get_starboard_emoji(storage_path: Path, guild_id: int | str) -> str:
    settings = get_guild_settings(storage_path, guild_id)
    emoji = str(settings.get("starboard_emoji") or "").strip()
    return emoji or "⭐"


def starboard_selfstar_allowed(storage_path: Path, guild_id: int | str) -> bool:
    settings = get_guild_settings(storage_path, guild_id)
    return bool(settings.get("starboard_allow_selfstar", True))


def starboard_ignores_nsfw(storage_path: Path, guild_id: int | str) -> bool:
    settings = get_guild_settings(storage_path, guild_id)
    return bool(settings.get("starboard_ignore_nsfw", False))


# ==============================
# moderation log helpers
# ==============================
def is_mod_log_enabled(storage_path: Path, guild_id: int | str) -> bool:
    settings = get_guild_settings(storage_path, guild_id)
    return bool(settings.get("mod_log_enabled", False)) and bool(settings.get("mod_log_channel_id"))


def is_mod_log_event_enabled(storage_path: Path, guild_id: int | str, kind: str) -> bool:
    """Whether this kind of moderation action ("warn"/"mute"/"kick"/"ban")
    should be posted to the mod log. Unknown kinds are always logged so a new
    action type can't silently disappear because the filter predates it."""
    if kind not in MOD_LOG_EVENT_KINDS:
        return True
    settings = get_guild_settings(storage_path, guild_id)
    enabled = settings.get("mod_log_events")
    if not isinstance(enabled, list):
        return True
    return kind in {str(value).lower() for value in enabled}


def dm_on_mod_action_enabled(storage_path: Path, guild_id: int | str) -> bool:
    settings = get_guild_settings(storage_path, guild_id)
    return bool(settings.get("dm_on_mod_action", True))


# ==============================
# welcome helpers
# ==============================
def get_welcome_delete_after(storage_path: Path, guild_id: int | str) -> int:
    settings = get_guild_settings(storage_path, guild_id)
    try:
        return max(0, int(settings.get("welcome_delete_after", 0) or 0))
    except (TypeError, ValueError):
        return 0


# ==============================
# embed color helpers
# ==============================
def parse_hex_color(value: str | None) -> int | None:
    """Parse "#RRGGBB" (or "RRGGBB") into an int; None for empty/invalid."""
    text = str(value or "").strip().lstrip("#")
    if len(text) != 6:
        return None
    try:
        return int(text, 16)
    except ValueError:
        return None


def get_embed_color_value(storage_path: Path, guild_id: int | str) -> int | None:
    settings = get_guild_settings(storage_path, guild_id)
    return parse_hex_color(settings.get("embed_color"))


# ==============================
# music limit helpers
# ==============================
def get_music_max_queue_length(storage_path: Path, guild_id: int | str) -> int:
    """Per-guild queue cap; 0 means "no guild-specific cap" (the music cog's
    hard MAX_QUEUE_SIZE still applies)."""
    settings = get_guild_settings(storage_path, guild_id)
    try:
        return max(0, int(settings.get("music_max_queue_length", 0) or 0))
    except (TypeError, ValueError):
        return 0


def get_music_max_track_seconds(storage_path: Path, guild_id: int | str) -> int:
    """Per-guild track length cap in seconds; 0 means "no guild-specific cap"."""
    settings = get_guild_settings(storage_path, guild_id)
    try:
        return max(0, int(settings.get("music_max_track_minutes", 0) or 0)) * 60
    except (TypeError, ValueError):
        return 0


def get_music_djadmin_role_id(storage_path: Path, guild_id: int | str) -> int | None:
    """Role whose members may always control music playback, even with DJ
    mode on. Empty/invalid means no such role is configured."""
    settings = get_guild_settings(storage_path, guild_id)
    role_id = settings.get("music_djadmin_role_id")
    if not role_id:
        return None
    try:
        return int(role_id)
    except (TypeError, ValueError):
        return None


def get_mod_log_channel_id(storage_path: Path, guild_id: int | str) -> int | None:
    settings = get_guild_settings(storage_path, guild_id)
    channel_id = settings.get("mod_log_channel_id")
    if not channel_id:
        return None
    try:
        return int(channel_id)
    except (TypeError, ValueError):
        return None
