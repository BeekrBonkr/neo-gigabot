"""Modules: the bot's features as things a server turns on and off.

Every module has one boolean setting (its flag). Features that need more than
the flag to do anything (a channel, a role) stay idle until that is set too.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from utils.settings import get_guild_settings

# module -> (flag setting, display name, top-level commands that belong to it)
MODULES: dict[str, tuple[str, str, tuple[str, ...]]] = {
    "welcome": ("welcome_enabled", "Welcome and leave", ()),
    "member_roles": ("member_roles_enabled", "Member roles", ()),
    "moderation": ("moderation_enabled", "Moderation", (
        "kick", "ban", "tempban", "unban", "softban", "slowmode", "mute", "unmute", "warn", "warnings",
        "purge", "clean", "lock", "unlock", "prune", "modhistory", "note",
    )),
    "automod": ("automod_enabled", "Automod", ()),
    "antiraid": ("raid_protection_enabled", "Anti-raid", ("lockdown",)),
    "verification": ("verify_enabled", "Verification", ()),
    "honeypot": ("honeypot_enabled", "Honeypot", ()),
    "appeals": ("appeals_enabled", "Ban appeals", ()),
    "modlog": ("mod_log_enabled", "Mod log", ()),
    "logging": ("logging_enabled", "Activity log", ()),
    "suggestions": ("suggestions_enabled", "Suggestions", ("suggest",)),
    "tempvoice": ("tempvoice_enabled", "Temporary voice channels", ()),
    "starboard": ("starboard_enabled", "Starboard", ()),
    "music": ("music_enabled", "Music", ("music", "playlist")),
    "tags": ("tags_enabled", "Tags", ("tag", "tags")),
    "stickies": ("stickies_enabled", "Sticky messages", ("stick", "stickembed", "stickslow", "stickstop", "stickstart", "stickremove", "getstickies")),
    "rolemenus": ("rolemenus_enabled", "Role menus", ("rolemenu",)),
    "reactionroles": ("reactionroles_enabled", "Reaction roles", ("reactionrole",)),
    "schedules": ("schedules_enabled", "Scheduled actions", ("schedule",)),
}
_COMMAND_MODULE = {command: module for module, (_, _, commands) in MODULES.items() for command in commands}


def enabled_in(settings: dict[str, Any], module: str) -> bool:
    return bool(settings.get(MODULES[module][0], True))


def module_enabled(storage_path: Path, guild_id: int | str, module: str) -> bool:
    return enabled_in(get_guild_settings(storage_path, guild_id), module)


def disabled_module_for(qualified_name: str, settings: dict[str, Any]) -> str | None:
    """The display name of the turned-off module a command belongs to, or None
    if the command may run."""
    root = qualified_name.split(" ", 1)[0].lower()
    module = _COMMAND_MODULE.get(root)
    if module is None or enabled_in(settings, module):
        return None
    return MODULES[module][1]
