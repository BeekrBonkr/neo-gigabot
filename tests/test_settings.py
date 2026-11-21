from __future__ import annotations

from pathlib import Path

import yaml

from utils.settings import (
    DEFAULT_GUILD_SETTINGS,
    clear_settings_caches,
    command_is_blocked,
    create_guild_settings,
    delete_guild_settings,
    dm_on_mod_action_enabled,
    get_default_settings,
    get_embed_color_value,
    get_guild_settings,
    get_music_djadmin_role_id,
    get_music_max_queue_length,
    get_music_max_track_seconds,
    get_starboard_emoji,
    get_welcome_delete_after,
    guild_settings_exists,
    is_bot_channel,
    is_mod_log_event_enabled,
    migrate_blocked_command_names,
    parse_hex_color,
    save_guild_settings,
    starboard_ignores_nsfw,
    starboard_selfstar_allowed,
    sync_all_guild_settings,
    sync_with_default,
    update_guild_settings,
)

GUILD_ID = 111222333


# ---------- sync_with_default (pure) ----------


def test_sync_adds_missing_keys() -> None:
    synced = sync_with_default({}, DEFAULT_GUILD_SETTINGS)
    assert synced == DEFAULT_GUILD_SETTINGS
    assert synced is not DEFAULT_GUILD_SETTINGS


def test_sync_drops_unknown_keys() -> None:
    synced = sync_with_default({"prefix": "!", "autoroles": [1]}, DEFAULT_GUILD_SETTINGS)
    assert "prefix" not in synced
    assert synced["autoroles"] == [1]


def test_sync_keeps_existing_values() -> None:
    synced = sync_with_default({"music_default_volume": 80}, DEFAULT_GUILD_SETTINGS)
    assert synced["music_default_volume"] == 80


def test_sync_recurses_into_nested_dicts() -> None:
    default = {"outer": {"a": 1, "b": 2}}
    synced = sync_with_default({"outer": {"a": 9, "junk": True}}, default)
    assert synced == {"outer": {"a": 9, "b": 2}}


def test_sync_does_not_alias_default_containers() -> None:
    synced = sync_with_default({}, DEFAULT_GUILD_SETTINGS)
    synced["autoroles"].append(42)
    assert DEFAULT_GUILD_SETTINGS["autoroles"] == []


# ---------- default settings file ----------


def test_get_default_settings_creates_file(storage: Path) -> None:
    defaults = get_default_settings(storage)
    assert defaults == DEFAULT_GUILD_SETTINGS
    assert (storage / "settings" / "default.yml").is_file()


def test_default_yml_unknown_keys_are_preserved(storage: Path) -> None:
    # _merge_missing_defaults keeps keys found on disk even if they're not in
    # the built-in schema - that's why removing a schema key also requires
    # cleaning default.yml. Pin that behavior down.
    path = storage / "settings" / "default.yml"
    path.parent.mkdir(parents=True)
    with path.open("w", encoding="utf-8") as handle:
        yaml.safe_dump({"custom_key": "kept", "warn_threshold": 3}, handle)

    defaults = get_default_settings(storage)
    assert defaults["custom_key"] == "kept"
    assert defaults["warn_threshold"] == 3
    assert defaults["autoroles"] == []  # missing keys filled from built-ins


# ---------- guild settings round-trips ----------


def test_get_creates_row_with_defaults(storage: Path) -> None:
    assert not guild_settings_exists(storage, GUILD_ID)
    settings = get_guild_settings(storage, GUILD_ID)
    assert settings == DEFAULT_GUILD_SETTINGS
    assert guild_settings_exists(storage, GUILD_ID)


def test_save_and_reload(storage: Path) -> None:
    settings = get_guild_settings(storage, GUILD_ID)
    settings["blocked_commands"] = ["snipe"]
    save_guild_settings(storage, GUILD_ID, settings)
    assert get_guild_settings(storage, GUILD_ID)["blocked_commands"] == ["snipe"]


def test_save_strips_unknown_keys(storage: Path) -> None:
    settings = get_guild_settings(storage, GUILD_ID)
    settings["made_up"] = True
    save_guild_settings(storage, GUILD_ID, settings)
    assert "made_up" not in get_guild_settings(storage, GUILD_ID)


def test_update_patch(storage: Path) -> None:
    updated = update_guild_settings(storage, GUILD_ID, {"warn_threshold": 5})
    assert updated["warn_threshold"] == 5
    assert get_guild_settings(storage, GUILD_ID)["warn_threshold"] == 5


def test_create_and_delete(storage: Path) -> None:
    create_guild_settings(storage, GUILD_ID)
    assert guild_settings_exists(storage, GUILD_ID)
    delete_guild_settings(storage, GUILD_ID)
    assert not guild_settings_exists(storage, GUILD_ID)
    # And a fresh read after delete comes back as pure defaults, not stale cache.
    assert get_guild_settings(storage, GUILD_ID) == DEFAULT_GUILD_SETTINGS


def test_sync_all_guild_settings(storage: Path) -> None:
    import json

    from utils.db import write_transaction
    from utils.settings import ensure_settings_database

    # Plant a raw row with a legacy key and a missing key, bypassing the helpers.
    ensure_settings_database(storage)
    with write_transaction(storage) as connection:
        connection.execute(
            "INSERT INTO guild_settings (guild_id, data) VALUES (?, ?)",
            (GUILD_ID, json.dumps({"prefix": "!", "warn_threshold": 2})),
        )

    assert sync_all_guild_settings(storage) == 1
    synced = get_guild_settings(storage, GUILD_ID)
    assert "prefix" not in synced
    assert synced["warn_threshold"] == 2
    # Second run is a no-op.
    assert sync_all_guild_settings(storage) == 0


# ---------- caching behavior ----------


def test_cached_read_returns_a_copy(storage: Path) -> None:
    first = get_guild_settings(storage, GUILD_ID)
    first["autoroles"].append(999)  # mutate without saving
    second = get_guild_settings(storage, GUILD_ID)
    assert second["autoroles"] == []


def test_cache_survives_db_deletion_until_cleared(storage: Path) -> None:
    get_guild_settings(storage, GUILD_ID)
    (storage / "settings.db").unlink()
    # Cache still serves the old value...
    assert get_guild_settings(storage, GUILD_ID) == DEFAULT_GUILD_SETTINGS
    # ...and clearing it forces a re-read (which recreates the row).
    clear_settings_caches()
    assert get_guild_settings(storage, GUILD_ID) == DEFAULT_GUILD_SETTINGS


# ---------- convenience helpers ----------


def test_command_is_blocked_case_insensitive(storage: Path) -> None:
    update_guild_settings(storage, GUILD_ID, {"blocked_commands": ["SNIPE"]})
    assert command_is_blocked(storage, GUILD_ID, "snipe")
    assert command_is_blocked(storage, GUILD_ID, "Snipe")
    assert not command_is_blocked(storage, GUILD_ID, "poll")


def test_migrate_blocked_commands_expands_leaf_names(storage: Path) -> None:
    update_guild_settings(storage, GUILD_ID, {"blocked_commands": ["Add", "playlist play", "ghost"]})
    mapping = {
        "add": ["playlist add", "reactionrole add"],
        "play": ["play"],
    }

    assert migrate_blocked_command_names(storage, mapping) == 1
    migrated = get_guild_settings(storage, GUILD_ID)["blocked_commands"]
    # Leaf name expands to every qualified match (preserving old behavior),
    # already-qualified entries pass through, unknown names are kept.
    assert migrated == ["ghost", "playlist add", "playlist play", "reactionrole add"]

    # Second run is a no-op.
    assert migrate_blocked_command_names(storage, mapping) == 0
    # Guilds with nothing blocked are untouched.
    create_guild_settings(storage, GUILD_ID + 1)
    assert migrate_blocked_command_names(storage, mapping) == 0


def test_is_bot_channel_defaults_to_true(storage: Path) -> None:
    assert is_bot_channel(storage, GUILD_ID, 42)
    update_guild_settings(storage, GUILD_ID, {"bot_channels": [42]})
    assert is_bot_channel(storage, GUILD_ID, 42)
    assert not is_bot_channel(storage, GUILD_ID, 43)


def test_mod_log_event_filter(storage: Path) -> None:
    # Everything logged by default.
    for kind in ("warn", "mute", "kick", "ban"):
        assert is_mod_log_event_enabled(storage, GUILD_ID, kind)
    update_guild_settings(storage, GUILD_ID, {"mod_log_events": ["ban"]})
    assert is_mod_log_event_enabled(storage, GUILD_ID, "ban")
    assert not is_mod_log_event_enabled(storage, GUILD_ID, "warn")
    # Unknown kinds are never filtered out.
    assert is_mod_log_event_enabled(storage, GUILD_ID, "brand_new_kind")


def test_dm_on_mod_action_default_and_toggle(storage: Path) -> None:
    assert dm_on_mod_action_enabled(storage, GUILD_ID)
    update_guild_settings(storage, GUILD_ID, {"dm_on_mod_action": False})
    assert not dm_on_mod_action_enabled(storage, GUILD_ID)


def test_starboard_option_helpers(storage: Path) -> None:
    assert get_starboard_emoji(storage, GUILD_ID) == "⭐"
    assert starboard_selfstar_allowed(storage, GUILD_ID)
    assert not starboard_ignores_nsfw(storage, GUILD_ID)
    update_guild_settings(
        storage,
        GUILD_ID,
        {"starboard_emoji": "🔥", "starboard_allow_selfstar": False, "starboard_ignore_nsfw": True},
    )
    assert get_starboard_emoji(storage, GUILD_ID) == "🔥"
    assert not starboard_selfstar_allowed(storage, GUILD_ID)
    assert starboard_ignores_nsfw(storage, GUILD_ID)
    # Blank emoji falls back to the default rather than matching nothing.
    update_guild_settings(storage, GUILD_ID, {"starboard_emoji": "  "})
    assert get_starboard_emoji(storage, GUILD_ID) == "⭐"


def test_welcome_delete_after(storage: Path) -> None:
    assert get_welcome_delete_after(storage, GUILD_ID) == 0
    update_guild_settings(storage, GUILD_ID, {"welcome_delete_after": 30})
    assert get_welcome_delete_after(storage, GUILD_ID) == 30
    update_guild_settings(storage, GUILD_ID, {"welcome_delete_after": "junk"})
    assert get_welcome_delete_after(storage, GUILD_ID) == 0


def test_music_djadmin_role_id(storage: Path) -> None:
    assert get_music_djadmin_role_id(storage, GUILD_ID) is None
    update_guild_settings(storage, GUILD_ID, {"music_djadmin_role_id": "987654321"})
    assert get_music_djadmin_role_id(storage, GUILD_ID) == 987654321
    update_guild_settings(storage, GUILD_ID, {"music_djadmin_role_id": "junk"})
    assert get_music_djadmin_role_id(storage, GUILD_ID) is None


def test_parse_hex_color() -> None:
    assert parse_hex_color("#5865F2") == 0x5865F2
    assert parse_hex_color("5865f2") == 0x5865F2
    assert parse_hex_color("") is None
    assert parse_hex_color(None) is None
    assert parse_hex_color("#12345") is None
    assert parse_hex_color("zzzzzz") is None


def test_get_embed_color_value(storage: Path) -> None:
    assert get_embed_color_value(storage, GUILD_ID) is None
    update_guild_settings(storage, GUILD_ID, {"embed_color": "#FF8800"})
    assert get_embed_color_value(storage, GUILD_ID) == 0xFF8800


def test_music_limits(storage: Path) -> None:
    assert get_music_max_queue_length(storage, GUILD_ID) == 0
    assert get_music_max_track_seconds(storage, GUILD_ID) == 0
    update_guild_settings(storage, GUILD_ID, {"music_max_queue_length": 25, "music_max_track_minutes": 10})
    assert get_music_max_queue_length(storage, GUILD_ID) == 25
    assert get_music_max_track_seconds(storage, GUILD_ID) == 600


def test_the_command_pages_fit_in_one_dropdown_and_lose_no_command(tmp_path):
    import asyncio

    import discord
    from discord import app_commands
    from discord.ext import commands

    import main
    from cogs.settings import MAX_SELECT_OPTIONS, UNBLOCKABLE_COMMAND_NAMES, _blockable_command_pages

    async def load():
        bot = commands.Bot(command_prefix="!", intents=discord.Intents.none(), help_command=None)
        bot.project_root = main.Path(main.__file__).resolve().parent
        bot.storage_path = tmp_path
        for extension in main.COGS:
            await bot.load_extension(extension)
        pages = [(label, [command.qualified_name for command in chunk]) for label, chunk in _blockable_command_pages(bot)]
        everything = {c.qualified_name for c in bot.tree.walk_commands() if isinstance(c, app_commands.Command)}
        for extension in main.COGS:
            await bot.unload_extension(extension)
        return pages, everything

    pages, everything = asyncio.run(load())
    assert len(pages) <= MAX_SELECT_OPTIONS, [label for label, _ in pages]
    assert all(1 <= len(names) <= MAX_SELECT_OPTIONS for _, names in pages)
    listed = [name for _, names in pages for name in names]
    assert len(listed) == len(set(listed))
    missing = {n for n in everything - set(listed) if n.split(" ")[0] not in ("owner", "settings") and n.split(" ")[-1] not in UNBLOCKABLE_COMMAND_NAMES}
    assert not missing, sorted(missing)
    print([(label, len(names)) for label, names in pages])


def test_editing_the_leave_message_turns_leave_messages_on(storage):
    from utils.settings import get_guild_settings, update_guild_settings

    update_guild_settings(storage, 5, {"welcome_channel_ids": ["555"]})
    assert get_guild_settings(storage, 5)["leave_enabled"] is False
    update_guild_settings(storage, 5, {"leave_message": "Bye {user}"})
    assert get_guild_settings(storage, 5)["leave_enabled"] is True
    update_guild_settings(storage, 6, {"leave_message": ""})   # clearing it turns nothing on
    assert get_guild_settings(storage, 6)["leave_enabled"] is False


def test_the_dashboard_reports_modules_as_the_bot_treats_them(storage):
    from types import SimpleNamespace

    from cogs.settings import SettingsDashboardView
    from utils.settings import get_guild_settings, update_guild_settings

    view = SimpleNamespace(_on_off=SettingsDashboardView._on_off)
    state = lambda settings, module, configured, detail=None: SettingsDashboardView._module_state(view, settings, module, configured, detail)  # noqa: E731
    update_guild_settings(storage, 7, {"welcome_channel_ids": ["555"]})
    assert state(get_guild_settings(storage, 7), "welcome", True, "<#555>").startswith("🟢 On")
    update_guild_settings(storage, 7, {"welcome_enabled": False})  # as the website's switch does
    assert "switched off on the website" in state(get_guild_settings(storage, 7), "welcome", True, "<#555>")
    assert state(get_guild_settings(storage, 7), "welcome", False) == "⚫ Off"
