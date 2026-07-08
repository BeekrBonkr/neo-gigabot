from __future__ import annotations

from pathlib import Path

from utils.command_log import rotate_command_logs
from utils.playlists import ensure_playlists_table
from utils.reactionroles import ensure_reaction_roles_table
from utils.settings import (
    ensure_default_settings_file,
    ensure_settings_database,
    sync_all_guild_settings,
)
from utils.starboard import ensure_starboard_table
from utils.tempbans import ensure_tempbans_table
from utils.warnings import ensure_warnings_table

REQUIRED_DIRS = [
    "settings",
    "logs",
    "cache",
    "temp",
]


def ensure_storage_layout(storage_path: Path) -> None:
    storage_path.mkdir(parents=True, exist_ok=True)

    for name in REQUIRED_DIRS:
        (storage_path / name).mkdir(parents=True, exist_ok=True)

    ensure_default_settings_file(storage_path)
    ensure_settings_database(storage_path)
    sync_all_guild_settings(storage_path)
    ensure_playlists_table(storage_path)
    ensure_warnings_table(storage_path)
    ensure_reaction_roles_table(storage_path)
    ensure_tempbans_table(storage_path)
    ensure_starboard_table(storage_path)
    rotate_command_logs(storage_path)
