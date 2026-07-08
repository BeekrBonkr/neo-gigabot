from __future__ import annotations

from pathlib import Path

import pytest

from utils.playlists import (
    MAX_NAME_LENGTH,
    MAX_PLAYLISTS_PER_USER,
    MAX_SONGS_PER_PLAYLIST,
    PlaylistError,
    add_songs,
    create_playlist,
    delete_playlist,
    get_playlist,
    get_visible_playlist,
    import_playlist_raw,
    list_liked_playlists,
    list_playlists,
    remove_song,
    rename_playlist,
    set_status,
    toggle_like,
)

OWNER = 100
OTHER = 200


def track(title: str = "Song", url: str = "https://example.com/watch?v=1") -> dict:
    return {"title": title, "webpage_url": url, "uploader": "Someone", "duration": 123, "thumbnail": None}


def test_create_and_get_round_trip(storage: Path) -> None:
    created = create_playlist(storage, OWNER, "MyMix")
    assert created["name"] == "mymix"
    assert created["display_name"] == "MyMix"

    fetched = get_playlist(storage, OWNER, "MYMIX")  # lookup is case-insensitive
    assert fetched is not None
    assert fetched["display_name"] == "MyMix"


def test_duplicate_name_rejected(storage: Path) -> None:
    create_playlist(storage, OWNER, "mix")
    with pytest.raises(PlaylistError):
        create_playlist(storage, OWNER, "MIX")


@pytest.mark.parametrize("bad", ["", "has space", "way_too_long_name_over_20_chars", "emoji💥"])
def test_invalid_names_rejected(storage: Path, bad: str) -> None:
    with pytest.raises(PlaylistError):
        create_playlist(storage, OWNER, bad)


def test_playlist_cap_per_user(storage: Path) -> None:
    for i in range(MAX_PLAYLISTS_PER_USER):
        create_playlist(storage, OWNER, f"list{i}")
    with pytest.raises(PlaylistError):
        create_playlist(storage, OWNER, "onemore")
    # The cap is per-user, not global.
    create_playlist(storage, OTHER, "theirs")


def test_add_songs_respects_playlist_cap(storage: Path) -> None:
    create_playlist(storage, OWNER, "mix")
    # Fill to exactly the cap in chunks (add_songs also caps per-call).
    added_total = 0
    while added_total < MAX_SONGS_PER_PLAYLIST:
        _, added = add_songs(storage, OWNER, "mix", [track(f"t{added_total + i}") for i in range(10)])
        added_total += added
    assert added_total == MAX_SONGS_PER_PLAYLIST
    with pytest.raises(PlaylistError):
        add_songs(storage, OWNER, "mix", [track("overflow")])


def test_remove_song_positions(storage: Path) -> None:
    create_playlist(storage, OWNER, "mix")
    add_songs(storage, OWNER, "mix", [track("a"), track("b"), track("c")])
    updated, removed = remove_song(storage, OWNER, "mix", 2)
    assert removed["title"] == "b"
    assert [s["title"] for s in updated["songs"]] == ["a", "c"]
    with pytest.raises(PlaylistError):
        remove_song(storage, OWNER, "mix", 5)


def test_sanitize_track_requires_url(storage: Path) -> None:
    create_playlist(storage, OWNER, "mix")
    with pytest.raises(PlaylistError):
        add_songs(storage, OWNER, "mix", [{"title": "no url"}])


def test_rename_collision_and_success(storage: Path) -> None:
    create_playlist(storage, OWNER, "one")
    create_playlist(storage, OWNER, "two")
    with pytest.raises(PlaylistError):
        rename_playlist(storage, OWNER, "one", "TWO")
    renamed = rename_playlist(storage, OWNER, "one", "Uno")
    assert renamed["name"] == "uno"
    assert get_playlist(storage, OWNER, "one") is None
    assert get_playlist(storage, OWNER, "uno") is not None


def test_private_playlists_hidden_from_others(storage: Path) -> None:
    create_playlist(storage, OWNER, "secret")
    set_status(storage, OWNER, "secret", "private")

    assert get_visible_playlist(storage, OWNER, "secret", viewer_id=OWNER)["name"] == "secret"
    with pytest.raises(PlaylistError):
        get_visible_playlist(storage, OWNER, "secret", viewer_id=OTHER)
    with pytest.raises(PlaylistError):
        toggle_like(storage, OWNER, "secret", OTHER)


def test_toggle_like_and_liked_listing(storage: Path) -> None:
    create_playlist(storage, OWNER, "mix")
    _, now_liked = toggle_like(storage, OWNER, "mix", OTHER)
    assert now_liked
    liked = list_liked_playlists(storage, OTHER)
    assert [p["name"] for p in liked] == ["mix"]

    _, now_liked = toggle_like(storage, OWNER, "mix", OTHER)
    assert not now_liked
    assert list_liked_playlists(storage, OTHER) == []


def test_toggle_like_desired_pins_the_outcome(storage: Path) -> None:
    create_playlist(storage, OWNER, "mix")

    # Unliking something never liked must stay unliked (not toggle to liked).
    _, now_liked = toggle_like(storage, OWNER, "mix", OTHER, desired=False)
    assert not now_liked
    assert list_liked_playlists(storage, OTHER) == []

    # desired=True is idempotent: no double-likes.
    toggle_like(storage, OWNER, "mix", OTHER, desired=True)
    playlist, now_liked = toggle_like(storage, OWNER, "mix", OTHER, desired=True)
    assert now_liked
    assert playlist["likes"].count(str(OTHER)) == 1

    _, now_liked = toggle_like(storage, OWNER, "mix", OTHER, desired=False)
    assert not now_liked
    assert list_liked_playlists(storage, OTHER) == []


def test_delete_playlist(storage: Path) -> None:
    create_playlist(storage, OWNER, "mix")
    delete_playlist(storage, OWNER, "mix")
    assert list_playlists(storage, OWNER) == []
    with pytest.raises(PlaylistError):
        delete_playlist(storage, OWNER, "mix")


# ---------- migration-only raw import ----------


def test_import_raw_bypasses_caps(storage: Path) -> None:
    for i in range(MAX_PLAYLISTS_PER_USER):
        create_playlist(storage, OWNER, f"list{i}")
    imported = import_playlist_raw(storage, OWNER, "legacy", songs=[track(f"t{i}") for i in range(3)])
    assert imported["name"] == "legacy"
    assert len(list_playlists(storage, OWNER)) == MAX_PLAYLISTS_PER_USER + 1


def test_import_raw_coerces_illegal_names(storage: Path) -> None:
    imported = import_playlist_raw(storage, OWNER, "my cool playlist from 2019!!")
    assert " " not in imported["name"]
    assert len(imported["display_name"]) <= MAX_NAME_LENGTH


def test_import_raw_suffixes_collisions(storage: Path) -> None:
    first = import_playlist_raw(storage, OWNER, "legacy")
    second = import_playlist_raw(storage, OWNER, "LEGACY")
    assert first["name"] != second["name"]
    assert len(list_playlists(storage, OWNER)) == 2
