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
