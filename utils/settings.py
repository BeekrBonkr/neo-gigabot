from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any

import yaml

from utils.db import connect as _db_connect
from utils import site_access

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
    "music_autoplay": False,
    "web_player_enabled": True,
    "web_join_enabled": True,
    "web_discovery_enabled": True,
    # Automod: each rule is off, or deletes the message and optionally warns or mutes.
    "automod_invites_action": "off",
    "automod_words_action": "off",
    "automod_banned_words": "",
    "automod_mentions_action": "off",
    "automod_mention_limit": 6,
    "automod_spam_action": "off",
    "automod_spam_limit": 6,
    "automod_mute_duration": "10m",
    "automod_exempt_roles": [],
    "automod_exempt_channels": [],
    # Anti-raid: a burst of joins starts a lockdown, during which new joiners are kicked.
    "antiraid_enabled": False,
    "antiraid_join_limit": 10,
    "antiraid_window_seconds": 30,
    "antiraid_lockdown_minutes": 10,
    "antiraid_min_account_age_days": 0,
    # Verification gate: a button that grants a role.
    "verify_role_id": "",
    "verify_min_account_age_days": 0,
    "verify_message": "Welcome! Press the button below to get access to the rest of the server.",
    # Honeypot: a channel nobody should post in. Whoever does is removed.
    "honeypot_channel_id": "",
    "honeypot_action": "ban",
    "honeypot_exempt_roles": [],
    "honeypot_exempt_users": [],
    # Ban appeals: banned users can answer these questions on the website.
    "appeals_enabled": False,
    "appeals_questions": "",  # the older one-question-per-line form; appeals_form wins when set
    "appeals_form": [],
    "appeals_intro": "",
    "appeals_reappeal_days": 7,
    "appeals_channel_id": "",
    # How long the website's searchable copy of the activity log is kept. 0 keeps no copy.
    "logging_keep_days": 30,
    # Which Discord permission opens each part of the server's pages on the website (utils/site_access.py).
    **{key: site_access.DEFAULT_PERMISSION for key in site_access.KEYS},
    # Where /suggest posts. Empty turns suggestions off.
    "suggestions_channel_id": "",
    # Richer welcomes: which forms the join greeting takes (any of text,
    # embed, image; all that are listed are posted), and an optional DM.
    "welcome_styles": ["text"],
    "welcome_embed_title": "Welcome to {server}!",
    "welcome_embed_color": "",
    "welcome_embed_image_url": "",
    "welcome_embed_footer": "",
    "welcome_dm_message": "",
    # The generated welcome image: a gradient or an uploaded picture behind
    # the avatar, and the two lines of text around the member's name.
    "welcome_card_background": "gradient",
    "welcome_card_gradient": "horizontal",
    "welcome_card_color_start": "",
    "welcome_card_color_end": "",
    "welcome_card_text_color": "",
    "welcome_card_dim": 45,
    "welcome_card_title": "WELCOME",
    "welcome_card_subtitle": "Member #{count} of {server}",
    # Temporary voice channels: joining the hub channel creates one.
    "tempvoice_hub_channel_id": "",
    "tempvoice_name": "{name}'s channel",
    "tempvoice_user_limit": 0,
    # Module switches (utils/modules.py). Features that work from commands are
    # on by default; features that need setting up start off.
    "welcome_enabled": False,
    "leave_enabled": False,
    "member_roles_enabled": False,
    "moderation_enabled": True,
    "automod_enabled": False,
    "raid_protection_enabled": False,
    "verify_enabled": False,
    "honeypot_enabled": False,
    "suggestions_enabled": False,
    "tempvoice_enabled": False,
    "starboard_enabled": False,
    "music_enabled": True,
    "tags_enabled": True,
    "stickies_enabled": True,
    "rolemenus_enabled": True,
    "reactionroles_enabled": True,
    "schedules_enabled": True,
    "snipe_enabled": True,
}

# Modules that are a single command: module switch -> that command.
COMMAND_MODULE_FLAGS: dict[str, str] = {"snipe": "snipe_enabled"}

# Module switches that start off, and the settings that mean the module is in
# use. Two jobs: a server saved before the switches existed keeps what it had
# configured (derive_module_flags), and setting a module up from Discord turns
# it on (update_guild_settings).
MODULE_TRIGGERS: dict[str, tuple[str, ...]] = {
    "welcome_enabled": ("welcome_channel_ids", "welcome_dm_message"),
    "leave_enabled": ("leave_channel_ids", "leave_message"),
    "member_roles_enabled": ("autoroles", "sticky_roles"),
    "automod_enabled": ("automod_invites_action", "automod_words_action", "automod_mentions_action", "automod_spam_action"),
    "raid_protection_enabled": ("antiraid_enabled", "antiraid_min_account_age_days"),
    "verify_enabled": ("verify_role_id",),
    "honeypot_enabled": ("honeypot_channel_id",),
    "suggestions_enabled": ("suggestions_channel_id",),
    "tempvoice_enabled": ("tempvoice_hub_channel_id",),
    "starboard_enabled": ("starboard_channel_id",),
}


def _in_use(value: Any) -> bool:
    return value not in ("", "off", 0, False, None) and value != []


def derive_module_flags(data: dict[str, Any]) -> dict[str, Any]:
    """Fill in module switches missing from stored settings: on when the
    module was already configured, so upgrading never turns a feature off."""
    missing = [flag for flag in MODULE_TRIGGERS if flag not in data]
    if not missing and "welcome_style" not in data:
        return data
    derived = dict(data)
    for flag in missing:
        derived[flag] = any(_in_use(data.get(key)) for key in MODULE_TRIGGERS[flag])
    if "leave_enabled" in missing:
        # Leave messages used to ride on the welcome switch and fall back to
        # the welcome channels, so a server that had them keeps them.
        has_channel = _in_use(data.get("leave_channel_ids")) or _in_use(data.get("welcome_channel_ids"))
        derived["leave_enabled"] = bool(derived.get("welcome_enabled")) and has_channel and _in_use(data.get("leave_message", "set"))
    if "welcome_style" in data:
        # One style became a list of them.
        style = derived.pop("welcome_style")
        if "welcome_styles" not in data and style in WELCOME_STYLES:
            derived["welcome_styles"] = [style]
    return derived

# How the newer settings are validated when they arrive from the web panel
# (cogs/panel_bridge.py validate_settings). Declared here, next to the
# defaults, so adding a setting is one edit in one file.
AUTOMOD_ACTIONS = ("off", "delete", "warn", "mute")
WELCOME_STYLES = ("text", "embed", "image")
SETTING_CHOICES: dict[str, tuple[str, ...]] = {
    **{key: site_access.PERMISSIONS for key in site_access.KEYS},
    "automod_invites_action": AUTOMOD_ACTIONS,
    "automod_words_action": AUTOMOD_ACTIONS,
    "automod_mentions_action": AUTOMOD_ACTIONS,
    "automod_spam_action": AUTOMOD_ACTIONS,
    "honeypot_action": ("ban", "softban", "kick"),
    "welcome_card_background": ("gradient", "image"),
    "welcome_card_gradient": ("horizontal", "vertical", "diagonal", "radial", "solid"),
}
# Lists that hold any of a fixed set of values, kept in this order.
SETTING_MULTI_CHOICES: dict[str, tuple[str, ...]] = {"welcome_styles": WELCOME_STYLES}
# Hex colors ("" for none) and https links ("" for none).
SETTING_COLORS: set[str] = {"welcome_embed_color", "welcome_card_color_start", "welcome_card_color_end", "welcome_card_text_color"}
SETTING_URLS: set[str] = {"welcome_embed_image_url"}
SETTING_DURATIONS: set[str] = {"automod_mute_duration"}
SETTING_ID_LISTS: set[str] = {"automod_exempt_roles", "automod_exempt_channels", "honeypot_exempt_roles", "honeypot_exempt_users"}
SETTING_IDS: set[str] = {"verify_role_id", "honeypot_channel_id", "suggestions_channel_id", "tempvoice_hub_channel_id", "appeals_channel_id"}
SETTING_INT_RANGES: dict[str, tuple[int, int]] = {
    "automod_mention_limit": (2, 50),
    "automod_spam_limit": (3, 30),
    "antiraid_join_limit": (3, 100),
    "antiraid_window_seconds": (5, 600),
    "antiraid_lockdown_minutes": (1, 1440),
    "antiraid_min_account_age_days": (0, 365),
    "verify_min_account_age_days": (0, 365),
    "logging_keep_days": (0, 90),
    "appeals_reappeal_days": (0, 365),
    "tempvoice_user_limit": (0, 99),
    "welcome_card_dim": (0, 90),
}
# Text settings that may be longer than the usual 1000 characters.
SETTING_TEXT_LIMITS: dict[str, int] = {"automod_banned_words": 2000, "welcome_card_title": 40, "welcome_card_subtitle": 80}

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

    synced = sync_with_default(derive_module_flags(loaded), defaults)
    if synced != loaded:
        _upsert_guild_settings(storage_path, guild_id, synced)
    else:
        _GUILD_CACHE[(str(storage_path), int(guild_id))] = deepcopy(synced)

    return synced


def sync_command_modules(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    """Keep a module that is one command (COMMAND_MODULE_FLAGS) and that
    command's entry in the disabled list saying the same thing: disabling the
    command turns the module off, and turning the module on re-enables it."""
    result = dict(after)
    for command, flag in COMMAND_MODULE_FLAGS.items():
        was_blocked = blocking_entry(command, before.get("blocked_commands")) is not None
        now_blocked = blocking_entry(command, result.get("blocked_commands")) is not None
        was_on, now_on = bool(before.get(flag, True)), bool(result.get(flag, True))
        if now_blocked and not was_blocked:
            result[flag] = False
        elif now_on and not was_on:
            result["blocked_commands"] = [name for name in (result.get("blocked_commands") or []) if str(name).lower() != command]
            if blocking_entry(command, result["blocked_commands"]) is not None:
                result[flag] = False  # still off through a wider entry; the switch must not claim otherwise
        elif was_blocked and not now_blocked:
            result[flag] = True
    return result


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
    # Setting a module up (choosing its channel, say) turns it on.
    for flag, triggers in MODULE_TRIGGERS.items():
        if flag not in patch and any(_in_use(patch.get(key)) for key in triggers if key in patch):
            updated[flag] = True
    updated = sync_command_modules(current, updated)
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
        synced = sync_with_default(derive_module_flags(loaded), defaults)
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
def blocking_entry(qualified_name: str, blocked: Any) -> str | None:
    """The blocked_commands entry that turns this command off, or None. An
    entry is a command's qualified name, or the name of a group, which turns
    off everything inside it ("music" covers "music play" and "music playlist
    add", including subcommands added later)."""
    names = {str(name).lower() for name in (blocked or [])}
    if not names:
        return None
    words = qualified_name.lower().split()
    for length in range(len(words), 0, -1):
        candidate = " ".join(words[:length])
        if candidate in names:
            return candidate
    return None


def command_is_blocked(storage_path: Path, guild_id: int | str, command_name: str) -> bool:
    settings = get_guild_settings(storage_path, guild_id)
    return blocking_entry(command_name, settings.get("blocked_commands")) is not None


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
