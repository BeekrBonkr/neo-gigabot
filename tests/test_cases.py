from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from utils import cases as store


def test_cases_are_listed_newest_first_and_scoped_to_the_guild(storage):
    first = store.add_case(storage, 1, 50, 9, "Member Warned", "spam")
    second = store.add_case(storage, 1, 50, 9, "Member Muted", None, "1h")
    store.add_case(storage, 1, 51, 9, "Member Kicked")
    store.add_case(storage, 2, 50, 9, "Member Banned")
    assert [c["id"] for c in store.list_cases(storage, 1, 50)] == [second, first]
    assert store.list_cases(storage, 1, 50)[0]["duration"] == "1h" and store.list_cases(storage, 1, 50)[0]["reason"] == ""
    assert len(store.list_cases(storage, 1)) == 3 and store.count_cases(storage, 1, 50) == 2
    assert len(store.list_cases(storage, 1, limit=1)) == 1


def test_notes_are_validated_capped_and_only_removable_in_their_guild(storage):
    note_id = store.add_note(storage, 1, 50, 9, "  watch for alts  ")
    assert store.list_notes(storage, 1, 50)[0]["note"] == "watch for alts"
    for bad in ("", "   ", "x" * (store.MAX_NOTE_LENGTH + 1)):
        with pytest.raises(store.CaseError):
            store.add_note(storage, 1, 50, 9, bad)
    assert store.remove_note(storage, 2, note_id) is None            # another server can't remove it
    assert store.remove_note(storage, 1, note_id)["note"] == "watch for alts"
    assert store.list_notes(storage, 1, 50) == []
    for n in range(store.MAX_NOTES_PER_MEMBER):
        store.add_note(storage, 1, 50, 9, f"n{n}")
    with pytest.raises(store.CaseError):
        store.add_note(storage, 1, 50, 9, "one too many")


def test_every_logged_action_becomes_a_case_even_without_a_mod_log(storage):
    from cogs.moderation import Moderation

    cog = Moderation.__new__(Moderation)
    cog.bot = SimpleNamespace(storage_path=storage)
    guild = SimpleNamespace(id=1, get_channel=lambda cid: None)
    asyncio.run(cog._log_mod_action(
        guild, event="mute", title="🔇 Member Muted", color=None,
        target=SimpleNamespace(id=50, mention="<@50>"), moderator=SimpleNamespace(id=9, mention="<@9>"), reason="loud", duration="10m",
    ))
    case = store.list_cases(storage, 1, 50)[0]
    assert (case["action"], case["reason"], case["duration"], case["moderator_id"]) == ("Member Muted", "loud", "10m", 9)
