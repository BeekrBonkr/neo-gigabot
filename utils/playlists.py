from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from utils.db import connect, write_transaction

MAX_PLAYLISTS_PER_USER = 5
MAX_SONGS_PER_PLAYLIST = 50
MAX_SONGS_ADDED_PER_CALL = 10
MAX_NAME_LENGTH = 20
MAX_DESCRIPTION_LENGTH = 200

VALID_STATUSES = ("public", "private")
NAME_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,%d}$" % MAX_NAME_LENGTH)

CREATE_PLAYLISTS_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS playlists (
    owner_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    display_name TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'public',
    songs TEXT NOT NULL DEFAULT '[]',
    likes TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (owner_id, name)
)
"""


class PlaylistError(ValueError):
    """User-facing playlist validation/state error. Message is safe to show as-is."""


def ensure_playlists_table(storage_path: Path) -> None:
    with connect(storage_path) as connection:
        connection.execute(CREATE_PLAYLISTS_TABLE_SQL)
        connection.commit()


def normalize_name(name: str) -> str:
    return (name or "").strip().lower()


def validate_name(name: str) -> str:
    candidate = (name or "").strip()
    if not NAME_PATTERN.match(candidate):
        raise PlaylistError(
            f"Playlist names must be 1-{MAX_NAME_LENGTH} characters and can only contain "
            "letters, numbers, underscores, and hyphens (no spaces)."
        )
    return candidate


def validate_description(description: str) -> str:
    text = (description or "").strip()
    if len(text) > MAX_DESCRIPTION_LENGTH:
        raise PlaylistError(f"Description must be {MAX_DESCRIPTION_LENGTH} characters or fewer.")
    return text


def validate_status(status: str) -> str:
    normalized = (status or "").strip().lower()
    if normalized not in VALID_STATUSES:
        raise PlaylistError("Status must be either `public` or `private`.")
    return normalized


def sanitize_track(track: dict[str, Any]) -> dict[str, Any]:
    webpage_url = str(track.get("webpage_url") or "").strip()
    if not webpage_url:
        raise PlaylistError("That track has no playable URL and can't be saved.")

    duration = track.get("duration")
    return {
        "title": str(track.get("title") or "Unknown title")[:200],
        "webpage_url": webpage_url[:500],
        "uploader": str(track.get("uploader"))[:100] if track.get("uploader") else None,
        "duration": int(duration) if isinstance(duration, (int, float)) else None,
        "thumbnail": str(track.get("thumbnail"))[:500] if track.get("thumbnail") else None,
    }


def _row_to_playlist(row) -> dict[str, Any]:
    return {
        "owner_id": int(row["owner_id"]),
        "name": row["name"],
        "display_name": row["display_name"],
        "description": row["description"],
        "status": row["status"],
        "songs": json.loads(row["songs"]),
        "likes": json.loads(row["likes"]),
    }


def _fetch_row(connection, owner_id: int, name_key: str):
    return connection.execute(
        "SELECT * FROM playlists WHERE owner_id = ? AND name = ?",
        (int(owner_id), name_key),
    ).fetchone()


def list_playlists(storage_path: Path, owner_id: int) -> list[dict[str, Any]]:
    ensure_playlists_table(storage_path)
    with connect(storage_path) as connection:
        rows = connection.execute(
            "SELECT * FROM playlists WHERE owner_id = ? ORDER BY display_name COLLATE NOCASE",
            (int(owner_id),),
        ).fetchall()
    return [_row_to_playlist(row) for row in rows]


def get_playlist(storage_path: Path, owner_id: int, name: str) -> dict[str, Any] | None:
    ensure_playlists_table(storage_path)
    key = normalize_name(name)
    with connect(storage_path) as connection:
        row = _fetch_row(connection, owner_id, key)
    return _row_to_playlist(row) if row else None


def get_visible_playlist(
    storage_path: Path,
    owner_id: int,
    name: str,
    *,
    viewer_id: int,
) -> dict[str, Any]:
    """Fetch a playlist, enforcing that private playlists are only visible to their owner."""
    playlist = get_playlist(storage_path, owner_id, name)
    if playlist is None:
        raise PlaylistError(f"No playlist named `{name}` was found for that user.")
    if playlist["status"] == "private" and int(viewer_id) != int(owner_id):
        raise PlaylistError("That playlist is private.")
    return playlist


def create_playlist(storage_path: Path, owner_id: int, name: str) -> dict[str, Any]:
    ensure_playlists_table(storage_path)
    display_name = validate_name(name)
    key = normalize_name(display_name)

    with write_transaction(storage_path) as connection:
        existing = _fetch_row(connection, owner_id, key)
        if existing is not None:
            raise PlaylistError(f"You already have a playlist named `{display_name}`.")

        count = connection.execute(
            "SELECT COUNT(*) FROM playlists WHERE owner_id = ?", (int(owner_id),)
        ).fetchone()[0]
        if count >= MAX_PLAYLISTS_PER_USER:
            raise PlaylistError(
                f"You already have the maximum of {MAX_PLAYLISTS_PER_USER} playlists. "
                "Delete one before creating another."
            )

        connection.execute(
            """
            INSERT INTO playlists (owner_id, name, display_name, description, status, songs, likes)
            VALUES (?, ?, ?, '', 'public', '[]', '[]')
            """,
            (int(owner_id), key, display_name),
        )

    return {
        "owner_id": int(owner_id),
        "name": key,
        "display_name": display_name,
        "description": "",
        "status": "public",
        "songs": [],
        "likes": [],
    }


def delete_playlist(storage_path: Path, owner_id: int, name: str) -> dict[str, Any]:
    ensure_playlists_table(storage_path)
    key = normalize_name(name)

    with write_transaction(storage_path) as connection:
        row = _fetch_row(connection, owner_id, key)
        if row is None:
            raise PlaylistError(f"You don't have a playlist named `{name}`.")
        connection.execute(
            "DELETE FROM playlists WHERE owner_id = ? AND name = ?",
            (int(owner_id), key),
        )
        return _row_to_playlist(row)


def rename_playlist(storage_path: Path, owner_id: int, name: str, new_name: str) -> dict[str, Any]:
    ensure_playlists_table(storage_path)
    old_key = normalize_name(name)
    new_display = validate_name(new_name)
    new_key = normalize_name(new_display)

    with write_transaction(storage_path) as connection:
        row = _fetch_row(connection, owner_id, old_key)
        if row is None:
            raise PlaylistError(f"You don't have a playlist named `{name}`.")

        if new_key != old_key:
            collision = _fetch_row(connection, owner_id, new_key)
            if collision is not None:
                raise PlaylistError(f"You already have a playlist named `{new_display}`.")

        connection.execute(
            """
            UPDATE playlists
            SET name = ?, display_name = ?, updated_at = CURRENT_TIMESTAMP
            WHERE owner_id = ? AND name = ?
            """,
            (new_key, new_display, int(owner_id), old_key),
        )
        updated = dict(_row_to_playlist(row))
        updated["name"] = new_key
        updated["display_name"] = new_display
        return updated


def set_description(storage_path: Path, owner_id: int, name: str, description: str) -> dict[str, Any]:
    ensure_playlists_table(storage_path)
    key = normalize_name(name)
    clean_description = validate_description(description)

    with write_transaction(storage_path) as connection:
        row = _fetch_row(connection, owner_id, key)
        if row is None:
            raise PlaylistError(f"You don't have a playlist named `{name}`.")
        connection.execute(
            "UPDATE playlists SET description = ?, updated_at = CURRENT_TIMESTAMP WHERE owner_id = ? AND name = ?",
            (clean_description, int(owner_id), key),
        )
        updated = dict(_row_to_playlist(row))
        updated["description"] = clean_description
        return updated


def set_status(storage_path: Path, owner_id: int, name: str, status: str) -> dict[str, Any]:
    ensure_playlists_table(storage_path)
    key = normalize_name(name)
    clean_status = validate_status(status)

    with write_transaction(storage_path) as connection:
        row = _fetch_row(connection, owner_id, key)
        if row is None:
            raise PlaylistError(f"You don't have a playlist named `{name}`.")
        connection.execute(
            "UPDATE playlists SET status = ?, updated_at = CURRENT_TIMESTAMP WHERE owner_id = ? AND name = ?",
            (clean_status, int(owner_id), key),
        )
        updated = dict(_row_to_playlist(row))
        updated["status"] = clean_status
        return updated


def add_songs(
    storage_path: Path,
    owner_id: int,
    name: str,
    tracks: list[dict[str, Any]],
) -> tuple[dict[str, Any], int]:
    """Append tracks to a playlist. Returns (updated playlist, number actually added)."""
    ensure_playlists_table(storage_path)
    key = normalize_name(name)
    sanitized = [sanitize_track(track) for track in tracks[:MAX_SONGS_ADDED_PER_CALL]]

    with write_transaction(storage_path) as connection:
        row = _fetch_row(connection, owner_id, key)
        if row is None:
            raise PlaylistError(f"You don't have a playlist named `{name}`.")

        songs = json.loads(row["songs"])
        available = MAX_SONGS_PER_PLAYLIST - len(songs)
        if available <= 0:
            raise PlaylistError(f"That playlist is already full ({MAX_SONGS_PER_PLAYLIST} songs max).")

        to_add = sanitized[:available]
        songs.extend(to_add)
        connection.execute(
            "UPDATE playlists SET songs = ?, updated_at = CURRENT_TIMESTAMP WHERE owner_id = ? AND name = ?",
            (json.dumps(songs), int(owner_id), key),
        )
        updated = dict(_row_to_playlist(row))
        updated["songs"] = songs
        return updated, len(to_add)


def remove_song(storage_path: Path, owner_id: int, name: str, position: int) -> tuple[dict[str, Any], dict[str, Any]]:
    """Remove a 1-indexed song from a playlist. Returns (updated playlist, removed track)."""
    ensure_playlists_table(storage_path)
    key = normalize_name(name)

    with write_transaction(storage_path) as connection:
        row = _fetch_row(connection, owner_id, key)
        if row is None:
            raise PlaylistError(f"You don't have a playlist named `{name}`.")

        songs = json.loads(row["songs"])
        if position < 1 or position > len(songs):
            raise PlaylistError(f"Invalid position. That playlist has {len(songs)} song(s).")

        removed = songs.pop(position - 1)
        connection.execute(
            "UPDATE playlists SET songs = ?, updated_at = CURRENT_TIMESTAMP WHERE owner_id = ? AND name = ?",
            (json.dumps(songs), int(owner_id), key),
        )
        updated = dict(_row_to_playlist(row))
        updated["songs"] = songs
        return updated, removed


def shuffle_playlist(storage_path: Path, owner_id: int, name: str) -> dict[str, Any]:
    import random

    ensure_playlists_table(storage_path)
    key = normalize_name(name)

    with write_transaction(storage_path) as connection:
        row = _fetch_row(connection, owner_id, key)
        if row is None:
            raise PlaylistError(f"You don't have a playlist named `{name}`.")

        songs = json.loads(row["songs"])
        random.shuffle(songs)
        connection.execute(
            "UPDATE playlists SET songs = ?, updated_at = CURRENT_TIMESTAMP WHERE owner_id = ? AND name = ?",
            (json.dumps(songs), int(owner_id), key),
        )
        updated = dict(_row_to_playlist(row))
        updated["songs"] = songs
        return updated


def toggle_like(
    storage_path: Path, owner_id: int, name: str, liker_id: int, *, desired: bool | None = None
) -> tuple[dict[str, Any], bool]:
    """Toggle a like from liker_id on owner_id's playlist. Returns (updated playlist, now_liked).

    `desired` pins the outcome instead of toggling: False always ends unliked
    (so /playlist unlike on something never liked can't *add* a like), True
    always ends liked. A no-op returns the playlist unchanged."""
    ensure_playlists_table(storage_path)
    key = normalize_name(name)

    with write_transaction(storage_path) as connection:
        row = _fetch_row(connection, owner_id, key)
        if row is None:
            raise PlaylistError("That user doesn't have a playlist with that name.")

        playlist = _row_to_playlist(row)
        if playlist["status"] == "private" and int(liker_id) != int(owner_id):
            raise PlaylistError("That playlist is private.")

        likes = playlist["likes"]
        liker_key = str(int(liker_id))
        currently_liked = liker_key in likes
        now_liked = (not currently_liked) if desired is None else desired
        if now_liked == currently_liked:
            return playlist, now_liked
        if now_liked:
            likes.append(liker_key)
        else:
            likes.remove(liker_key)

        connection.execute(
            "UPDATE playlists SET likes = ?, updated_at = CURRENT_TIMESTAMP WHERE owner_id = ? AND name = ?",
            (json.dumps(likes), int(owner_id), key),
        )
        playlist["likes"] = likes
        return playlist, now_liked


def list_liked_playlists(storage_path: Path, liker_id: int) -> list[dict[str, Any]]:
    """Return public playlists liked by liker_id, across all owners."""
    ensure_playlists_table(storage_path)
    liker_key = str(int(liker_id))
    with connect(storage_path) as connection:
        rows = connection.execute("SELECT * FROM playlists").fetchall()

    liked = []
    for row in rows:
        playlist = _row_to_playlist(row)
        if liker_key in playlist["likes"] and (
            playlist["status"] == "public" or playlist["owner_id"] == int(liker_id)
        ):
            liked.append(playlist)
    return liked


def import_playlist_raw(
    storage_path: Path,
    owner_id: int,
    display_name: str,
    *,
    description: str = "",
    status: str = "public",
    songs: list[dict[str, Any]] | None = None,
    likes: list[str] | None = None,
) -> dict[str, Any]:
    """Insert a playlist directly, bypassing the per-user/per-playlist caps that
    guard the live `/playlist create` and `/playlist add` commands against abuse.

    Only meant for one-time migration of pre-existing data that may already
    exceed those caps (e.g. a legacy user with 8 playlists, or one with 120 songs
    in a single playlist) - historical data shouldn't be silently truncated just
    because new limits exist to prevent *future* abuse. Do not call this from a
    live command path.
    """
    ensure_playlists_table(storage_path)

    if not NAME_PATTERN.match(display_name or ""):
        # Old names may not fit the new charset/length rules (accented
        # characters, spaces, etc.) - coerce instead of dropping the playlist.
        coerced = re.sub(r"[^A-Za-z0-9_-]", "_", display_name or "")[:MAX_NAME_LENGTH]
        display_name = coerced or "playlist"
    else:
        display_name = display_name[:MAX_NAME_LENGTH]

    clean_description = (description or "")[:MAX_DESCRIPTION_LENGTH]
    clean_status = status if status in VALID_STATUSES else "public"
    clean_songs = [sanitize_track(song) for song in (songs or [])]
    clean_likes = [str(int(uid)) for uid in (likes or []) if str(uid).strip().lstrip("-").isdigit()]

    with write_transaction(storage_path) as connection:
        key = normalize_name(display_name)
        final_display_name = display_name
        suffix = 2
        while _fetch_row(connection, owner_id, key) is not None:
            # Name collision, most likely two legacy names collapsing to the same
            # coerced/lowercased key - suffix rather than clobbering the earlier one.
            final_display_name = f"{display_name}_{suffix}"[:MAX_NAME_LENGTH]
            key = normalize_name(final_display_name)
            suffix += 1

        connection.execute(
            """
            INSERT INTO playlists (owner_id, name, display_name, description, status, songs, likes)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                int(owner_id),
                key,
                final_display_name,
                clean_description,
                clean_status,
                json.dumps(clean_songs),
                json.dumps(clean_likes),
            ),
        )

    return {
        "owner_id": int(owner_id),
        "name": key,
        "display_name": final_display_name,
        "description": clean_description,
        "status": clean_status,
        "songs": clean_songs,
        "likes": clean_likes,
    }
