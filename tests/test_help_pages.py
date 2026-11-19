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


def test_whatsnew_sums_up_then_shows_one_command_a_page_with_its_sample(monkeypatch) -> None:
    import datetime

    from cogs.help import WhatsNewView
    from utils import command_dates

    today = datetime.date.today().isoformat()
    monkeypatch.setattr(command_dates, "_dates", lambda: {f"image cmd{index}": today for index in range(12)} | {"music cmd0": "2026-01-05"})

    async def run() -> None:
        cog = _make_cog({"music": 2, "image": 12})

        async def get(name):
            return (b"gif-bytes", "boom.gif") if name == "image cmd9" else None  # cmd9 sorts first

        cog.bot.samples = SimpleNamespace(get=get)
        summary, files = await cog.build_whats_new(0)
        assert "**12** command(s)" in summary.description and "...and 2 more." in summary.description and not files
        assert "🆕" not in summary.description.split("\n", 2)[2]  # every line is new, so none needs the marker
        first, files = await cog.build_whats_new(1)
        assert first.title == "🆕 /image cmd9" and first.footer.text == "1 of 12" and first.image.url == "attachment://boom.gif"
        assert files[0].filename == "boom.gif" and files[0].fp.read() == b"gif-bytes"
        second, files = await cog.build_whats_new(2)
        assert second.image.url is None and not files  # no sample, no picture
        assert (await cog.build_whats_new(99))[0].footer.text == "12 of 12"
        previous, following = WhatsNewView(cog=cog, author_id=1).children
        assert previous.disabled and following.label == "Browse them ▶" and following.page == 1
        assert WhatsNewView(cog=cog, author_id=1, page=12).children[1].disabled
        monkeypatch.setattr(command_dates, "_dates", lambda: {})
        assert "Nothing new" in (await cog.build_whats_new(3))[0].description

    asyncio.run(run())
