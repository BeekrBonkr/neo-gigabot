from __future__ import annotations

from pathlib import Path

from utils.starboard import delete_starboard_post, get_starboard_post, save_starboard_post

GUILD = 1
MESSAGE = 2
STARBOARD_MESSAGE = 3


def test_save_and_get(storage: Path) -> None:
    assert get_starboard_post(storage, GUILD, MESSAGE) is None
    save_starboard_post(storage, GUILD, MESSAGE, STARBOARD_MESSAGE, 3)

    post = get_starboard_post(storage, GUILD, MESSAGE)
    assert post is not None
    assert post["starboard_message_id"] == STARBOARD_MESSAGE
    assert post["star_count"] == 3


def test_save_updates_count(storage: Path) -> None:
    save_starboard_post(storage, GUILD, MESSAGE, STARBOARD_MESSAGE, 3)
    save_starboard_post(storage, GUILD, MESSAGE, STARBOARD_MESSAGE, 7)
    assert get_starboard_post(storage, GUILD, MESSAGE)["star_count"] == 7


def test_posts_scoped_per_guild(storage: Path) -> None:
    save_starboard_post(storage, GUILD, MESSAGE, STARBOARD_MESSAGE, 3)
    assert get_starboard_post(storage, GUILD + 1, MESSAGE) is None


def test_delete(storage: Path) -> None:
    save_starboard_post(storage, GUILD, MESSAGE, STARBOARD_MESSAGE, 3)
    assert delete_starboard_post(storage, GUILD, MESSAGE) is True
    assert delete_starboard_post(storage, GUILD, MESSAGE) is False
    assert get_starboard_post(storage, GUILD, MESSAGE) is None
