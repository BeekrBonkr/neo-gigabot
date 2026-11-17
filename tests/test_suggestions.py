from __future__ import annotations

import asyncio
from types import SimpleNamespace

from cogs.suggestions import Suggestions, build_embed
from utils import suggestions as store


def test_numbers_count_per_server_and_votes_toggle_and_switch(storage):
    first = store.create(storage, 1, 5, "  more emojis  ")
    second = store.create(storage, 1, 6, "movie night")
    other = store.create(storage, 2, 5, "elsewhere")
    assert (first["number"], second["number"], other["number"], first["text"]) == (1, 2, 1, "more emojis")
    store.set_message(storage, first["id"], 7, 900)
    assert store.vote(storage, 900, 10, 1)[1] == "added"
    assert store.vote(storage, 900, 11, -1)[1] == "added"
    suggestion, outcome = store.vote(storage, 900, 10, -1)
    assert outcome == "switched" and store.tally(suggestion) == (0, 2)
    suggestion, outcome = store.vote(storage, 900, 10, -1)
    assert outcome == "removed" and store.tally(suggestion) == (0, 1)
    assert store.vote(storage, 12345, 10, 1) == (None, "missing")


def test_a_decision_closes_voting_and_is_scoped_to_the_server(storage):
    suggestion = store.create(storage, 1, 5, "more emojis")
    store.set_message(storage, suggestion["id"], 7, 900)
    store.vote(storage, 900, 10, 1)
    assert store.decide(storage, 2, 1, "approved", 9) is None
    decided = store.decide(storage, 1, 1, "approved", 9, " good idea ")
    assert decided["status"] == "approved" and decided["decision_reason"] == "good idea"
    closed, outcome = store.vote(storage, 900, 11, 1)
    assert outcome == "closed" and store.tally(closed) == (1, 0)
    embed = build_embed(store.by_number(storage, 1, 1), None)
    assert "✅" in embed.title and embed.fields[0].value == "👍 1   👎 0" and "Approved by <@9>" in embed.fields[1].value


def test_pressing_a_vote_button_updates_the_message(storage):
    suggestion = store.create(storage, 1, 5, "more emojis")
    store.set_message(storage, suggestion["id"], 7, 900)
    edits, replies = [], []

    async def edit_message(embed=None):
        edits.append(embed.fields[0].value)

    async def send_message(text, ephemeral=False):
        replies.append(text)

    cog = Suggestions(SimpleNamespace(storage_path=storage, get_user=lambda uid: None))
    interaction = SimpleNamespace(message=SimpleNamespace(id=900), user=SimpleNamespace(id=10),
                                  response=SimpleNamespace(edit_message=edit_message, send_message=send_message))
    asyncio.run(cog.handle_vote(interaction, 1))
    assert edits == ["👍 1   👎 0"]
    store.decide(storage, 1, 1, "denied", 9)
    asyncio.run(cog.handle_vote(interaction, 1))
    assert "Voting is closed" in replies[0] and len(edits) == 1
