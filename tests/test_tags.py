from __future__ import annotations

import pytest

from utils import tags as store


def test_tags_are_created_replaced_counted_and_deleted_per_server(storage):
    saved, created = store.set_tag(storage, 1, " Server Rules ", "  Be kind.  ", 9)
    assert created and saved["name"] == "server-rules" and saved["content"] == "Be kind."
    saved, created = store.set_tag(storage, 1, "SERVER-RULES", "Be kind. No spam.", 9)
    assert not created and saved["content"] == "Be kind. No spam."
    store.set_tag(storage, 2, "server-rules", "other server", 9)
    assert store.get_tag(storage, 1, "Server Rules", count_use=True)["content"] == "Be kind. No spam."
    assert store.get_tag(storage, 1, "server-rules")["uses"] == 1 and store.get_tag(storage, 2, "server-rules")["uses"] == 0
    assert store.get_tag(storage, 1, "nope") is None
    assert store.delete_tag(storage, 1, "server-rules")["name"] == "server-rules" and store.delete_tag(storage, 1, "server-rules") is None
    assert [t["name"] for t in store.list_tags(storage, 2)] == ["server-rules"]


def test_names_text_and_the_per_server_cap_are_validated(storage):
    for name, content in (("bad name!", "x"), ("", "x"), ("x" * 33, "x"), ("ok", "   "), ("ok", "x" * (store.MAX_CONTENT_LENGTH + 1))):
        with pytest.raises(store.TagError):
            store.set_tag(storage, 1, name, content, 9)
    for n in range(store.MAX_TAGS_PER_GUILD):
        store.set_tag(storage, 1, f"t{n}", "x", 9)
    with pytest.raises(store.TagError, match="already has"):
        store.set_tag(storage, 1, "one-more", "x", 9)
    assert store.set_tag(storage, 1, "t0", "edits still work at the cap", 9)[1] is False
