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
        assert first.title == "🆕 /image cmd9" and first.footer.text.startswith("1 of 12") and first.image.url == "attachment://boom.gif"
        assert files[0].filename == "boom.gif" and files[0].fp.read() == b"gif-bytes"
        second, files = await cog.build_whats_new(2)
        assert second.image.url is None and not files  # no sample, no picture
        assert (await cog.build_whats_new(99))[0].footer.text.startswith("12 of 12")
        previous, following = WhatsNewView(cog=cog).children
        assert previous.disabled and following.label == "Browse them ▶" and following.page == 1
        assert WhatsNewView(cog=cog, page=12).children[1].disabled
        monkeypatch.setattr(command_dates, "_dates", lambda: {})
        assert "Nothing new" in (await cog.build_whats_new(3))[0].description

    asyncio.run(run())


def test_help_lists_only_what_the_person_can_use_in_that_server(tmp_path):
    import asyncio
    from types import SimpleNamespace as NS

    import discord
    from discord.ext import commands

    import main
    from cogs import help as help_module
    from utils.settings import clear_settings_caches, update_guild_settings

    clear_settings_caches()

    async def scenario():
        bot = commands.Bot(command_prefix="!", intents=discord.Intents.none(), help_command=None)
        bot.project_root = main.Path(main.__file__).resolve().parent
        bot.storage_path = tmp_path
        bot.config = NS(website_url="", beta_build=False)
        from utils.embeds import EmbedManager

        bot.embeds = EmbedManager(bot)
        for extension in main.COGS:
            await bot.load_extension(extension)
        cog = bot.get_cog("Help")
        import sys

        help_module = sys.modules[type(cog).__module__]  # load_extension imports the cog afresh: use that copy's state
        guild = NS(id=4242)

        def listed(permissions):
            help_module._usable.set(help_module.usable_by(bot, guild, NS(guild_permissions=permissions)))
            names = {command.qualified_name for _key, _emoji, _label, groups in help_module.CATEGORY_DEFS for command in cog._commands_in(groups)}
            options = [option.label for option in help_module.HelpCategorySelect(cog).options]
            return names, options, cog.build_home_embed().description

        everything = len(listed(discord.Permissions.all())[0])
        member, member_options, member_home = listed(discord.Permissions(send_messages=True))
        assert "ban" not in member and "settings" not in member and "reactionrole create" not in member and "music autoplay" not in member
        assert {"ping", "music play", "filter invert", "about"} <= member and len(member) < everything
        assert "Moderation" in member_options          # /poll and other open commands still live there
        assert "Server Settings" not in member_options and "Reaction Roles & Role Menus" not in member_options
        assert "are left out" in member_home and "Server Settings" not in member_home

        moderator = listed(discord.Permissions(send_messages=True, ban_members=True, moderate_members=True))[0]
        assert {"ban", "warn", "mute"} <= moderator and "kick" not in moderator and "settings" not in moderator

        update_guild_settings(tmp_path, guild.id, {"blocked_commands": ["music", "ping"], "tags_enabled": False})
        admin, admin_options, _ = listed(discord.Permissions.all())
        assert "music play" not in admin and "ping" not in admin and "tag" not in admin and "settings" in admin
        assert "Music" not in admin_options

        help_module._usable.set(help_module.usable_by(bot, None, NS(guild_permissions=discord.Permissions.none())))
        assert help_module._usable.get() is None       # in DMs, or where the bot is not a member: everything is listed
        assert len({c.qualified_name for _k, _e, _l, groups in help_module.CATEGORY_DEFS for c in cog._commands_in(groups)}) >= everything
        for extension in main.COGS:
            await bot.unload_extension(extension)

    try:
        asyncio.run(scenario())
    finally:
        clear_settings_caches()


def test_the_website_still_shows_what_a_command_needs():
    from cogs.moderation import Moderation
    from utils.checks import required_permissions

    assert required_permissions(Moderation.role) == ["manage_roles"]     # a server-wide check
    assert required_permissions(Moderation.ban) == ["ban_members"]       # a plain has_permissions check
