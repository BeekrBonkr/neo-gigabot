from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from utils.migration import migrate_all
from utils.playlists import get_playlist
from utils.reactionroles import get_role_for_reaction
from utils.settings import get_guild_settings, guild_settings_exists

GUILD = 555


@pytest.fixture()
def legacy_root(tmp_path: Path) -> Path:
    root = tmp_path / "gigabot"
    (root / "storage" / "settings").mkdir(parents=True)
    return root


def write_yaml(path: Path, data: object) -> None:
    with path.open("w", encoding="utf-8") as handle:
        yaml.safe_dump(data, handle)


def test_settings_migration_maps_and_normalizes(legacy_root: Path, storage: Path) -> None:
    write_yaml(
        legacy_root / "storage" / "settings" / f"{GUILD}.yml",
        {
            "djmode": 1,  # renamed -> music_dj_enabled (coerced to bool)
            "bot_channels": None,  # null -> []
            "blocked_commands": ["snipe"],
            "levelling": True,  # descoped, dropped by schema sync
            "prefix": "!",  # dropped
        },
    )
    write_yaml(legacy_root / "storage" / "settings" / "default.yml", {"prefix": "!"})

    summary = migrate_all(legacy_root, storage)
    assert summary.ok
    assert summary.settings.migrated == 1
    assert summary.settings.skipped_files == ["default.yml"]

    settings = get_guild_settings(storage, GUILD)
    assert settings["music_dj_enabled"] is True
    assert settings["bot_channels"] == []
    assert settings["blocked_commands"] == ["snipe"]
    assert "levelling" not in settings
    assert "prefix" not in settings
    assert "djmode" not in settings


def test_settings_migration_records_bad_files(legacy_root: Path, storage: Path) -> None:
    bad = legacy_root / "storage" / "settings" / "123.yml"
    bad.write_text("- just\n- a\n- list\n", encoding="utf-8")

    summary = migrate_all(legacy_root, storage)
    assert not summary.ok
    assert summary.settings.migrated == 0
    assert summary.settings.failed[0][0] == "123.yml"


def test_playlist_migration(legacy_root: Path, storage: Path) -> None:
    write_yaml(
        legacy_root / "storage" / "playlists.yml",
        {
            "100": {
                "roadtrip": {
                    "description": "windows down",
                    "status": "public",
                    "songs": [
                        {"title": "Song A", "webpage_url": "https://example.com/a"},
                        {"title": "Song B", "webpage_url": "https://example.com/b"},
                    ],
                    "likes": ["200"],
                }
            },
            "not-a-user-id": {"junk": {}},
        },
    )

    summary = migrate_all(legacy_root, storage)
    assert summary.ok
    assert summary.playlists.playlists_migrated == 1
    assert summary.playlists.songs_migrated == 2

    playlist = get_playlist(storage, 100, "roadtrip")
    assert playlist is not None
    assert [s["title"] for s in playlist["songs"]] == ["Song A", "Song B"]
    assert playlist["likes"] == ["200"]


def test_reaction_role_migration_shape(legacy_root: Path, storage: Path) -> None:
    # Legacy shape: {guild: {channel: {message: {role_id: emoji}}}}
    write_yaml(
        legacy_root / "storage" / "reactionroles.yml",
        {
            str(GUILD): {
                "10": {
                    "20": {"30": "🎉", "31": "🔥"},
                    "21": {},  # no mappings -> skipped
                }
            }
        },
    )

    summary = migrate_all(legacy_root, storage)
    assert summary.ok
    assert summary.reaction_roles.mappings_migrated == 2
    assert summary.reaction_roles.empty_messages_skipped == 1

    assert get_role_for_reaction(storage, 20, "🎉") == 30
    assert get_role_for_reaction(storage, 20, "🔥") == 31


def test_dry_run_counts_without_writing(legacy_root: Path, storage: Path) -> None:
    write_yaml(legacy_root / "storage" / "settings" / f"{GUILD}.yml", {"blocked_commands": ["snipe"]})
    write_yaml(
        legacy_root / "storage" / "playlists.yml",
        {"100": {"mix": {"songs": [{"title": "A", "webpage_url": "https://example.com/a"}]}}},
    )
    write_yaml(legacy_root / "storage" / "reactionroles.yml", {str(GUILD): {"10": {"20": {"30": "🎉"}}}})

    summary = migrate_all(legacy_root, storage, dry_run=True)
    assert summary.ok
    assert summary.settings.migrated == 1
    assert summary.playlists.playlists_migrated == 1
    assert summary.reaction_roles.mappings_migrated == 1

    assert not guild_settings_exists(storage, GUILD)
    assert get_playlist(storage, 100, "mix") is None
    assert get_role_for_reaction(storage, 20, "🎉") is None


def test_missing_legacy_dirs_are_noop(legacy_root: Path, storage: Path) -> None:
    empty = legacy_root / "does-not-exist"
    summary = migrate_all(empty, storage)
    assert summary.ok
    assert summary.settings.migrated == 0
    assert summary.playlists.playlists_migrated == 0
    assert summary.reaction_roles.mappings_migrated == 0
