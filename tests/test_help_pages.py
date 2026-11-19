"""A category with a lot of commands is split into pages: one embed holding
all of /image and friends is over Discord's size limit and never arrives."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import discord
from discord import app_commands

from cogs.help import HELP_PAGE_CHARS, HELP_PAGE_LINES, Help, HelpPageButton, HelpView
from utils.embeds import EmbedManager


def _group(name: str, count: int) -> app_commands.Group:
    group = app_commands.Group(name=name, description="group")
    for index in range(count):
        async def callback(interaction: discord.Interaction) -> None: ...
        group.add_command(app_commands.Command(name=f"cmd{index}", description="x" * 90, callback=callback))
    return group


def _make_cog(sizes: dict[str, int]) -> Help:
    nodes = {name: _group(name, count) for name, count in sizes.items()}
    cog = Help.__new__(Help)
    cog.bot = SimpleNamespace(tree=SimpleNamespace(get_command=nodes.get), embeds=EmbedManager(), config=None)
    cog._mention_cooldowns = {}
    return cog


def test_long_category_is_cut_into_pages_that_fit() -> None:
    cog = _make_cog({"image": 25, "filter": 25, "animate": 25, "memegen": 25, "edit": 20})
    pages = cog.category_pages("images")
    assert len(pages) > 1
    assert sum(page.count("\n") + 1 for page in pages) == 120
    assert all(len(page) <= HELP_PAGE_CHARS and page.count("\n") < HELP_PAGE_LINES for page in pages)
    first, last = cog.build_category_embed("images", 0), cog.build_category_embed("images", 999)
    assert first.footer.text == f"Page 1 of {len(pages)}"
    assert last.footer.text == f"Page {len(pages)} of {len(pages)}" and last.description == pages[-1]


def test_short_category_has_one_page_and_no_page_buttons() -> None:
    async def run() -> None:
        cog = _make_cog({"music": 5, "image": 25, "edit": 15})
        assert cog.build_category_embed("music").footer.text is None
        short = HelpView(cog=cog, author_id=1, selected="music")
        assert not [item for item in short.children if isinstance(item, HelpPageButton)]
        previous, following = [i for i in HelpView(cog=cog, author_id=1, selected="images").children if isinstance(i, HelpPageButton)]
        assert previous.disabled and not following.disabled and following.page == 1

    asyncio.run(run())


def test_page_button_moves_to_that_page() -> None:
    async def run() -> None:
        cog = _make_cog({"image": 25, "edit": 15})
        edits: list[dict] = []

        async def edit_message(**kwargs):
            edits.append(kwargs)

        view = HelpView(cog=cog, author_id=1, selected="images")
        following = [i for i in view.children if isinstance(i, HelpPageButton)][1]
        await following.callback(SimpleNamespace(response=SimpleNamespace(edit_message=edit_message)))
        assert edits[0]["embed"].footer.text.startswith("Page 2 of")
        assert edits[0]["view"].selected == "images" and view.is_finished()

    asyncio.run(run())


def test_recent_commands_are_marked_and_gathered_under_whats_new(monkeypatch) -> None:
    import datetime

    from cogs.help import NEW_KEY, HelpCategorySelect
    from utils import command_dates

    today = datetime.date.today()
    dates = {"music cmd0": today.isoformat(), "image cmd1": (today - datetime.timedelta(days=5)).isoformat(),
             "music cmd1": (today - datetime.timedelta(days=45)).isoformat()}
    monkeypatch.setattr(command_dates, "_dates", lambda: dates)
    assert command_dates.is_new("music cmd0") and not command_dates.is_new("music cmd1") and not command_dates.is_new("music cmd2")

    async def run() -> None:
        cog = _make_cog({"music": 3, "image": 3})
        assert "**/music cmd0** 🆕 - " in cog.build_category_embed("music").description
        assert "**/music cmd1** - " in cog.build_category_embed("music").description  # too old for the marker
        assert [command.qualified_name for command in cog.new_commands()] == ["music cmd0", "image cmd1"]  # newest first
        assert cog.build_category_embed(NEW_KEY).description.count("\n") == 1
        assert "🆕 **What's New** - 2 command(s)" in cog.build_home_embed().description
        assert HelpCategorySelect(cog).options[0].value == NEW_KEY
        monkeypatch.setattr(command_dates, "_dates", lambda: {})
        assert "What's New" not in cog.build_home_embed().description and HelpCategorySelect(cog).options[0].value == "fun"

    asyncio.run(run())
