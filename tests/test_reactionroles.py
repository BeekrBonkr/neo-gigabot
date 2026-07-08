from __future__ import annotations

from pathlib import Path

import pytest

from utils.reactionroles import (
    MAX_MAPPINGS_PER_GUILD,
    ReactionRoleError,
    add_mapping,
    clear_message,
    clear_reaction_role_cache,
    count_for_guild,
    get_role_for_reaction,
    has_mappings,
    import_reaction_role_raw,
    list_for_guild,
    list_for_message,
    remove_mapping,
    remove_role_mappings,
)

GUILD = 1
CHANNEL = 2
MESSAGE = 3
ROLE = 4


def test_add_and_lookup(storage: Path) -> None:
    add_mapping(storage, GUILD, MESSAGE, CHANNEL, "🎉", ROLE)
    assert get_role_for_reaction(storage, MESSAGE, "🎉") == ROLE
    assert get_role_for_reaction(storage, MESSAGE, "🔥") is None
    assert get_role_for_reaction(storage, 999, "🎉") is None


def test_add_replaces_existing_emoji_mapping(storage: Path) -> None:
    add_mapping(storage, GUILD, MESSAGE, CHANNEL, "🎉", ROLE)
    add_mapping(storage, GUILD, MESSAGE, CHANNEL, "🎉", ROLE + 1)
    assert get_role_for_reaction(storage, MESSAGE, "🎉") == ROLE + 1
    assert count_for_guild(storage, GUILD) == 1


def test_remove_mapping(storage: Path) -> None:
    add_mapping(storage, GUILD, MESSAGE, CHANNEL, "🎉", ROLE)
    assert remove_mapping(storage, MESSAGE, "🎉") is True
    assert remove_mapping(storage, MESSAGE, "🎉") is False
    assert get_role_for_reaction(storage, MESSAGE, "🎉") is None


def test_clear_message(storage: Path) -> None:
    add_mapping(storage, GUILD, MESSAGE, CHANNEL, "🎉", ROLE)
    add_mapping(storage, GUILD, MESSAGE, CHANNEL, "🔥", ROLE + 1)
    add_mapping(storage, GUILD, MESSAGE + 1, CHANNEL, "🎉", ROLE)

    removed = clear_message(storage, MESSAGE)
    assert len(removed) == 2
    assert list_for_message(storage, MESSAGE) == []
    # Other messages untouched.
    assert get_role_for_reaction(storage, MESSAGE + 1, "🎉") == ROLE


def test_guild_cap_enforced_but_not_for_import(storage: Path) -> None:
    for i in range(MAX_MAPPINGS_PER_GUILD):
        add_mapping(storage, GUILD, MESSAGE + i, CHANNEL, "🎉", ROLE)
    with pytest.raises(ReactionRoleError):
        add_mapping(storage, GUILD, MESSAGE + MAX_MAPPINGS_PER_GUILD, CHANNEL, "🎉", ROLE)

    # Migration import bypasses the cap.
    import_reaction_role_raw(storage, GUILD, MESSAGE + MAX_MAPPINGS_PER_GUILD, CHANNEL, "🎉", ROLE)
    assert count_for_guild(storage, GUILD) == MAX_MAPPINGS_PER_GUILD + 1


def test_list_for_guild(storage: Path) -> None:
    add_mapping(storage, GUILD, MESSAGE, CHANNEL, "🎉", ROLE)
    add_mapping(storage, GUILD + 1, MESSAGE + 1, CHANNEL, "🎉", ROLE)
    assert len(list_for_guild(storage, GUILD)) == 1


def test_remove_role_mappings(storage: Path) -> None:
    add_mapping(storage, GUILD, MESSAGE, CHANNEL, "🎉", ROLE)
    add_mapping(storage, GUILD, MESSAGE, CHANNEL, "🔥", ROLE + 1)
    add_mapping(storage, GUILD, MESSAGE + 1, CHANNEL, "🎉", ROLE)
    add_mapping(storage, GUILD + 1, MESSAGE + 2, CHANNEL, "🎉", ROLE)  # other guild, same role id

    assert remove_role_mappings(storage, GUILD, ROLE) == 2
    # MESSAGE still has the 🔥 mapping; MESSAGE+1 is now empty.
    assert get_role_for_reaction(storage, MESSAGE, "🔥") == ROLE + 1
    assert has_mappings(storage, MESSAGE)
    assert not has_mappings(storage, MESSAGE + 1)
    # Other guild untouched.
    assert get_role_for_reaction(storage, MESSAGE + 2, "🎉") == ROLE
    assert remove_role_mappings(storage, GUILD, ROLE) == 0


def test_has_mappings(storage: Path) -> None:
    assert not has_mappings(storage, MESSAGE)
    add_mapping(storage, GUILD, MESSAGE, CHANNEL, "🎉", ROLE)
    assert has_mappings(storage, MESSAGE)
    clear_message(storage, MESSAGE)
    assert not has_mappings(storage, MESSAGE)


def test_message_id_cache_stays_consistent(storage: Path) -> None:
    # get_role_for_reaction short-circuits through a message-id cache; make sure
    # every mutator keeps that cache in sync even after it's been populated.
    assert get_role_for_reaction(storage, MESSAGE, "🎉") is None  # primes the cache

    add_mapping(storage, GUILD, MESSAGE, CHANNEL, "🎉", ROLE)
    assert get_role_for_reaction(storage, MESSAGE, "🎉") == ROLE

    remove_mapping(storage, MESSAGE, "🎉")
    assert get_role_for_reaction(storage, MESSAGE, "🎉") is None

    add_mapping(storage, GUILD, MESSAGE, CHANNEL, "🔥", ROLE)
    clear_message(storage, MESSAGE)
    assert get_role_for_reaction(storage, MESSAGE, "🔥") is None

    # And a cold cache rebuilds correctly from the DB.
    add_mapping(storage, GUILD, MESSAGE, CHANNEL, "🎉", ROLE)
    clear_reaction_role_cache()
    assert get_role_for_reaction(storage, MESSAGE, "🎉") == ROLE
