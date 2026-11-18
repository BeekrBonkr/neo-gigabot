from __future__ import annotations

import json

from utils.db import connect
from utils.modules import MODULES, disabled_module_for, module_enabled
from utils.settings import (
    DEFAULT_GUILD_SETTINGS,
    clear_settings_caches,
    ensure_settings_database,
    get_guild_settings,
    update_guild_settings,
)

GUILD_ID = 42


def _store_raw(storage, data: dict) -> None:
    ensure_settings_database(storage)
    with connect(storage) as connection:
        connection.execute("INSERT INTO guild_settings (guild_id, data) VALUES (?, ?)", (GUILD_ID, json.dumps(data)))
        connection.commit()
    clear_settings_caches()


def test_every_module_has_a_boolean_flag_in_the_defaults():
    for flag, _, _ in MODULES.values():
        assert isinstance(DEFAULT_GUILD_SETTINGS[flag], bool), flag


def test_settings_saved_before_the_switches_keep_what_was_configured(storage):
    old = {key: value for key, value in DEFAULT_GUILD_SETTINGS.items() if not key.endswith("_enabled") or key in ("logging_enabled", "mod_log_enabled", "antiraid_enabled", "appeals_enabled", "music_dj_enabled")}
    old.update(welcome_channel_ids=[5], honeypot_channel_id="9", automod_spam_action="warn", antiraid_min_account_age_days=3)
    _store_raw(storage, old)
    settings = get_guild_settings(storage, GUILD_ID)
    assert settings["welcome_enabled"] and settings["honeypot_enabled"] and settings["automod_enabled"] and settings["raid_protection_enabled"]
    assert not settings["verify_enabled"] and not settings["starboard_enabled"] and not settings["member_roles_enabled"]
    assert settings["tags_enabled"] and settings["music_enabled"]  # command features start on


def test_a_switch_that_was_turned_off_stays_off(storage):
    update_guild_settings(storage, GUILD_ID, {"welcome_channel_ids": [5]})
    update_guild_settings(storage, GUILD_ID, {"welcome_enabled": False})
    clear_settings_caches()
    assert get_guild_settings(storage, GUILD_ID)["welcome_enabled"] is False


def test_setting_a_module_up_from_discord_turns_it_on(storage):
    assert not module_enabled(storage, GUILD_ID, "starboard")
    update_guild_settings(storage, GUILD_ID, {"starboard_channel_id": "77"})
    assert module_enabled(storage, GUILD_ID, "starboard")
    # Clearing the channel, or an explicit switch in the same change, is left alone.
    update_guild_settings(storage, GUILD_ID, {"suggestions_channel_id": ""})
    assert not module_enabled(storage, GUILD_ID, "suggestions")
    update_guild_settings(storage, GUILD_ID, {"verify_role_id": "3", "verify_enabled": False})
    assert not module_enabled(storage, GUILD_ID, "verification")


def test_commands_of_a_module_that_is_off_are_refused():
    settings = dict(DEFAULT_GUILD_SETTINGS, tags_enabled=False, moderation_enabled=False)
    assert disabled_module_for("tag", settings) == "Tags"
    assert disabled_module_for("tags set", settings) == "Tags"
    assert disabled_module_for("warnings view", settings) == "Moderation"
    assert disabled_module_for("music play", settings) is None
    assert disabled_module_for("poll", settings) is None  # not part of any module
    assert disabled_module_for("honeypot set", settings) is None  # setup commands always work


def test_a_group_entry_disables_everything_inside_it():
    from utils.settings import blocking_entry

    blocked = ["music", "image text caption", "Poll"]
    assert blocking_entry("music play", blocked) == "music" and blocking_entry("music playlist add", blocked) == "music"
    assert blocking_entry("image text caption", blocked) == "image text caption" and blocking_entry("image text quote", blocked) is None
    assert blocking_entry("poll", blocked) == "poll" and blocking_entry("musical", blocked) is None
    assert blocking_entry("ping", []) is None and blocking_entry("ping", None) is None


def test_snipe_is_a_module_tied_to_its_command(storage):
    assert module_enabled(storage, GUILD_ID, "snipe")  # on unless a server turns it off
    assert disabled_module_for("snipe", dict(DEFAULT_GUILD_SETTINGS, snipe_enabled=False)) == "Snipe"
    # Disabling the command switches the module off...
    update_guild_settings(storage, GUILD_ID, {"blocked_commands": ["snipe", "ping"]})
    assert not module_enabled(storage, GUILD_ID, "snipe")
    # ...switching the module back on re-enables the command, and nothing else...
    settings = update_guild_settings(storage, GUILD_ID, {"snipe_enabled": True})
    assert settings["snipe_enabled"] and settings["blocked_commands"] == ["ping"]
    # ...and switching the module off leaves the list alone: the module check refuses the command by itself.
    settings = update_guild_settings(storage, GUILD_ID, {"snipe_enabled": False})
    assert settings["blocked_commands"] == ["ping"]
    # Re-enabling the command from the list switches the module on too.
    update_guild_settings(storage, GUILD_ID, {"blocked_commands": ["snipe"]})
    assert update_guild_settings(storage, GUILD_ID, {"blocked_commands": []})["snipe_enabled"] is True


def test_deleted_messages_are_not_kept_where_sniping_is_off(storage):
    import asyncio
    from types import SimpleNamespace

    from cogs.fun import Fun

    cog = Fun.__new__(Fun)
    cog.bot = SimpleNamespace(storage_path=storage)
    cog._snipes = {}
    avatar = SimpleNamespace(url="https://cdn.discordapp.com/a.png")
    author = SimpleNamespace(bot=False, display_avatar=avatar, __str__=lambda: "robin")
    message = SimpleNamespace(author=author, guild=SimpleNamespace(id=GUILD_ID), channel=SimpleNamespace(id=7), content="oops", attachments=[])
    asyncio.run(cog.on_message_delete(message))
    assert cog._snipes[7]["content"] == "oops"
    update_guild_settings(storage, GUILD_ID, {"snipe_enabled": False})
    asyncio.run(cog.on_message_delete(message))
    assert 7 not in cog._snipes  # nothing new is kept, and what was held is dropped
    update_guild_settings(storage, GUILD_ID, {"snipe_enabled": True, "blocked_commands": ["fun stuff", "snipe"]})
    asyncio.run(cog.on_message_delete(message))
    assert 7 not in cog._snipes
