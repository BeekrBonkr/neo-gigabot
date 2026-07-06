from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from utils.playlists import import_playlist_raw
from utils.reactionroles import import_reaction_role_raw
from utils.settings import save_guild_settings

# Legacy per-guild settings keys that map straight across to the new schema
# unchanged (same name, same meaning). Anything NOT in this set (or in
# _RENAMED_KEYS below) gets silently dropped by save_guild_settings's own
# schema-sync - level_roles/levelling/xp_multiplier (leveling was descoped),
# rr (a static help string, not real data), suggestion_channel_ids
# (suggestions were descoped), and join_messages (a typo'd duplicate of
# join_message in some legacy files) all fall away this way with no special
# casing needed here.
_RENAMED_KEYS = {
    "djmode": "music_dj_enabled",
}

# A handful of legacy files stored these as `null` instead of an empty list.
_LIST_KEYS_TO_NORMALIZE = (
    "bot_channels",
    "blocked_commands",
    "ignored_log_channels",
    "ignored_users",
    "welcome_channel_ids",
    "autoroles",
)


@dataclass(slots=True)
class SettingsMigrationResult:
    migrated: int = 0
    skipped_files: list[str] = field(default_factory=list)
    failed: list[tuple[str, str]] = field(default_factory=list)


@dataclass(slots=True)
class PlaylistMigrationResult:
    playlists_migrated: int = 0
    songs_migrated: int = 0
    failed: list[tuple[str, str]] = field(default_factory=list)


@dataclass(slots=True)
class ReactionRoleMigrationResult:
    mappings_migrated: int = 0
    empty_messages_skipped: int = 0
    failed: list[tuple[str, str]] = field(default_factory=list)


@dataclass(slots=True)
class MigrationSummary:
    legacy_root: Path
    dry_run: bool
    settings: SettingsMigrationResult
    playlists: PlaylistMigrationResult
    reaction_roles: ReactionRoleMigrationResult

    @property
    def ok(self) -> bool:
        return not self.settings.failed and not self.playlists.failed and not self.reaction_roles.failed


def default_legacy_path(project_root: Path) -> Path:
    """Best-effort default: the old and new bots were laid out as sibling
    directories (gigabot_recode/gigabot, gigabot_recode/neo-gigabot)."""
    return project_root.parent / "gigabot"


def _map_legacy_guild_settings(raw: dict[str, Any]) -> dict[str, Any]:
    mapped = dict(raw)

    for old_key, new_key in _RENAMED_KEYS.items():
        if old_key in mapped:
            value = mapped.pop(old_key)
            mapped.setdefault(new_key, bool(value))

    for key in _LIST_KEYS_TO_NORMALIZE:
        if mapped.get(key) is None:
            mapped[key] = []

    return mapped


def migrate_guild_settings(legacy_root: Path, storage_path: Path, *, dry_run: bool = False) -> SettingsMigrationResult:
    result = SettingsMigrationResult()
    settings_dir = legacy_root / "storage" / "settings"
    if not settings_dir.is_dir():
        return result

    for path in sorted(settings_dir.glob("*.yml")):
        if not path.stem.isdigit():
            # e.g. a stray "default.yml" or anything non-numeric - not a real guild file.
            result.skipped_files.append(path.name)
            continue

        guild_id = int(path.stem)
        try:
            with path.open("r", encoding="utf-8") as handle:
                raw = yaml.safe_load(handle) or {}
            if not isinstance(raw, dict):
                raise ValueError("File did not contain a YAML mapping.")

            mapped = _map_legacy_guild_settings(raw)
            if not dry_run:
                save_guild_settings(storage_path, guild_id, mapped)
            result.migrated += 1
        except Exception as exc:  # noqa: BLE001 - one bad file shouldn't abort the whole migration
            result.failed.append((path.name, str(exc)))

    return result


def migrate_playlists(legacy_root: Path, storage_path: Path, *, dry_run: bool = False) -> PlaylistMigrationResult:
    result = PlaylistMigrationResult()
    playlists_path = legacy_root / "storage" / "playlists.yml"
    if not playlists_path.is_file():
        return result

    try:
        with playlists_path.open("r", encoding="utf-8") as handle:
            raw = yaml.safe_load(handle) or {}
    except Exception as exc:  # noqa: BLE001
        result.failed.append((playlists_path.name, str(exc)))
        return result

    if not isinstance(raw, dict):
        result.failed.append((playlists_path.name, "File did not contain a YAML mapping."))
        return result

    for owner_key, owned_playlists in raw.items():
        if not str(owner_key).isdigit() or not isinstance(owned_playlists, dict):
            continue
        owner_id = int(owner_key)

        for playlist_name, data in owned_playlists.items():
            if not isinstance(data, dict):
                continue
            label = f"{owner_key}/{playlist_name}"
            try:
                songs = data.get("songs") or []
                if not dry_run:
                    import_playlist_raw(
                        storage_path,
                        owner_id,
                        str(playlist_name),
                        description=str(data.get("description") or ""),
                        status=str(data.get("status") or "public"),
                        songs=songs,
                        likes=data.get("likes") or [],
                    )
                result.playlists_migrated += 1
                result.songs_migrated += len(songs)
            except Exception as exc:  # noqa: BLE001
                result.failed.append((label, str(exc)))

    return result


def migrate_reaction_roles(legacy_root: Path, storage_path: Path, *, dry_run: bool = False) -> ReactionRoleMigrationResult:
    """Legacy shape: {guild_id: {channel_id: {message_id: {role_id: emoji}}}}.

    (The innermost mapping is keyed by role ID with the emoji as the value,
    not the other way around - confirmed against the real legacy file, where
    the numeric keys are role snowflakes and the string values are emoji.)

    Migrating the mapping into SQLite is all that's needed for the feature to
    keep working - Discord persists a message's reactions independently of
    which bot added them, so the bot's own reaction from when the old bot
    added it is still sitting on the message. Nothing here needs a live
    Discord connection.
    """
    result = ReactionRoleMigrationResult()
    path = legacy_root / "storage" / "reactionroles.yml"
    if not path.is_file():
        return result

    try:
        with path.open("r", encoding="utf-8") as handle:
            raw = yaml.safe_load(handle) or {}
    except Exception as exc:  # noqa: BLE001
        result.failed.append((path.name, str(exc)))
        return result

    if not isinstance(raw, dict):
        result.failed.append((path.name, "File did not contain a YAML mapping."))
        return result

    for guild_key, channels in raw.items():
        if not str(guild_key).isdigit() or not isinstance(channels, dict):
            continue
        guild_id = int(guild_key)

        for channel_key, messages in channels.items():
            if not str(channel_key).isdigit() or not isinstance(messages, dict):
                continue
            channel_id = int(channel_key)

            for message_key, role_emojis in messages.items():
                if not str(message_key).isdigit() or not isinstance(role_emojis, dict):
                    continue
                message_id = int(message_key)

                if not role_emojis:
                    result.empty_messages_skipped += 1
                    continue

                for role_key, emoji in role_emojis.items():
                    label = f"{guild_key}/{channel_key}/{message_key}/{role_key}"
                    try:
                        if not str(role_key).isdigit():
                            raise ValueError(f"role id {role_key!r} is not numeric")
                        if not dry_run:
                            import_reaction_role_raw(
                                storage_path, guild_id, message_id, channel_id, str(emoji), int(role_key)
                            )
                        result.mappings_migrated += 1
                    except Exception as exc:  # noqa: BLE001
                        result.failed.append((label, str(exc)))

    return result


def migrate_all(legacy_root: Path, storage_path: Path, *, dry_run: bool = False) -> MigrationSummary:
    return MigrationSummary(
        legacy_root=legacy_root,
        dry_run=dry_run,
        settings=migrate_guild_settings(legacy_root, storage_path, dry_run=dry_run),
        playlists=migrate_playlists(legacy_root, storage_path, dry_run=dry_run),
        reaction_roles=migrate_reaction_roles(legacy_root, storage_path, dry_run=dry_run),
    )
