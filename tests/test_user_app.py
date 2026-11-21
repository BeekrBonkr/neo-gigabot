"""The bot as a personal app: which commands are opened to it, and the checks
on each use outside a server that has the bot."""

import asyncio
import types

import discord
from discord import app_commands
from discord.ext import commands

import main
from utils import user_app
from utils.embeds import EmbedManager
from utils.imaging import NO_HISTORY_MESSAGE, ImageToolkit
from utils.settings import _fetch_raw_guild_settings, update_guild_settings

HOME, FOREIGN, MEMBER, STRANGER = 4242, 9999, 1, 2


def loaded_tree(tmp_path, body):
    async def load():
        bot = commands.Bot(command_prefix="!", intents=discord.Intents.none(), help_command=None)
        bot.project_root = main.Path(main.__file__).resolve().parent
        bot.storage_path = tmp_path
        for extension in main.COGS:
            await bot.load_extension(extension)
        try:
            return body(bot.tree)
        finally:
            for extension in main.COGS:  # stops the background loops the cogs started
                await bot.unload_extension(extension)

    return asyncio.run(load())


def test_the_opened_commands_are_the_agreed_ones(tmp_path):
    def body(tree):
        slash = {c.qualified_name for c in tree.walk_commands() if isinstance(c, app_commands.Command)}
        menus = {m.name for m in tree.get_commands(type=discord.AppCommandType.message)}
        roots = {c.name for c in tree.get_commands()}
        # nothing in the lists has been renamed or removed out from under them
        assert user_app.COMMANDS <= slash and user_app.SERVER_ONLY <= slash and user_app.MENUS <= menus
        assert user_app.GROUPS | set(user_app.PARTIAL_GROUPS) <= roots
        for names in user_app.PARTIAL_GROUPS.values():
            assert names <= slash
        opened = {name for name in slash if user_app.works_as_user_app(name)}
        return opened, slash, {name for name in menus if user_app.works_as_user_app(name, menu=True)}

    opened, slash, menus = loaded_tree(tmp_path, body)
    assert len(opened) == 94 + 20 + 13 and menus == {"Edit Image", "Quote Message", "Set Image Source", "Clear Image Source"}
    assert {"filter invert", "image effects deepfry", "memegen drake", "trivia", "hug", "music playlist create", "music playlist liked"} <= opened
    closed = {"image text quote", "image misc clearimagesource", "music play", "music playlist play", "music savenow", "insult", "kiss", "kill",
              "reddit", "cursed", "help", "whatsnew", "greenhouse", "server", "snipe", "userinfo", "kick", "settings", "owner restart", "tag"}
    assert closed <= slash and not closed & opened


def test_marking_opens_whole_top_level_commands_and_nothing_else(tmp_path):
    def body(tree):
        user_app.mark_tree(tree)
        payload = {c.name: c.to_dict(tree) for c in tree.get_commands()}
        payload |= {m.name: m.to_dict(tree) for m in tree.get_commands(type=discord.AppCommandType.message)}
        return {name: (entry.get("integration_types"), entry.get("contexts")) for name, entry in payload.items()}

    marked = loaded_tree(tmp_path, body)
    for name in ("image", "filter", "edit", "animate", "memegen", "music", "ping", "about", "Edit Image", "Quote Message", "Set Image Source"):
        assert marked[name] == ([0, 1], [0, 1, 2]), name
    for name in ("kick", "owner", "help", "reddit", "insult", "settings", "greenhouse", "tag"):
        assert marked[name][0] is None and 2 not in (marked[name][1] or []), name  # server installs only, as before
    assert sum(1 for installs, _ in marked.values() if installs) == 20 + 5 + 1 + 4


class Refusals:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    async def error_interaction(self, interaction, title, description, **kwargs) -> None:
        self.sent.append((title, description))


def make_bot(storage=None):
    home = types.SimpleNamespace(id=HOME, chunked=True, get_member=lambda user_id: object() if user_id == MEMBER else None)
    return types.SimpleNamespace(
        guilds=[home], embeds=Refusals(), storage_path=storage, application_id=77, config=None,
        user=types.SimpleNamespace(name="Neo-GigaBot"),
    )


def interaction(user_id, *, guild_id=None, bot_is_member=False, private=False):
    return types.SimpleNamespace(
        user=types.SimpleNamespace(id=user_id), guild_id=guild_id, channel=types.SimpleNamespace(id=5),
        guild=types.SimpleNamespace(id=guild_id, filesize_limit=1) if guild_id else None,
        context=types.SimpleNamespace(private_channel=private),
        is_guild_integration=lambda: guild_id is not None and bot_is_member,
        type=discord.InteractionType.application_command,
    )


def command(name, menu=False):
    return types.SimpleNamespace(qualified_name=name) if not menu else app_commands.ContextMenu(name=name, callback=_menu)


async def _menu(interaction: discord.Interaction, message: discord.Message) -> None: ...


def test_placement():
    assert user_app.placement(interaction(MEMBER, guild_id=HOME, bot_is_member=True)) == user_app.SERVER
    assert user_app.placement(interaction(MEMBER, guild_id=FOREIGN)) == user_app.ELSEWHERE
    assert user_app.placement(interaction(MEMBER, private=True)) == user_app.ELSEWHERE
    assert user_app.placement(interaction(MEMBER)) == user_app.BOT_DM
    assert user_app.home_guild(interaction(MEMBER, guild_id=FOREIGN)) is None
    assert user_app.home_guild(interaction(MEMBER, guild_id=HOME, bot_is_member=True)).id == HOME


def test_gate():
    def allowed(who, name, menu=False, **where):
        bot = make_bot()
        ok = asyncio.run(user_app.gate(bot, interaction(who, **where), command(name, menu)))
        return ok, [title for title, _ in bot.embeds.sent]

    # in a server that has the bot, nothing changes: not for strangers, not for server-only commands
    assert allowed(STRANGER, "music play", guild_id=HOME, bot_is_member=True) == (True, [])
    # anywhere else, only people who share a server with the bot
    for where in ({"guild_id": FOREIGN}, {"private": True}, {}):
        assert allowed(STRANGER, "filter invert", **where) == (False, ["Join A Server First"])
        assert allowed(MEMBER, "filter invert", **where) == (True, [])
        assert allowed(MEMBER, "music playlist view", **where) == (True, [])
        assert allowed(MEMBER, "Edit Image", menu=True, **where) == (True, [])
    # and outside the bot's own DMs, only the commands opened for it
    for name in ("music play", "music playlist play", "image text quote", "insult", "help", "owner restart"):
        assert allowed(MEMBER, name, guild_id=FOREIGN) == (False, ["Server Only"]), name
        assert allowed(MEMBER, name, private=True) == (False, ["Server Only"]), name
        assert allowed(MEMBER, name) == (True, []), name  # the bot's DMs work as they always have

    bot = make_bot()
    asyncio.run(user_app.gate(bot, interaction(STRANGER, guild_id=FOREIGN), command("ping")))
    assert "discord.com/oauth2/authorize?client_id=77" in bot.embeds.sent[0][1] and "Neo-GigaBot" in bot.embeds.sent[0][1]


def test_a_member_list_that_is_not_loaded_is_asked_about():
    async def fetch_member(user_id):
        if user_id != MEMBER:
            raise discord.NotFound(types.SimpleNamespace(status=404, reason="Not Found"), "unknown member")
        return object()

    bot = make_bot()
    bot.guilds = [types.SimpleNamespace(id=HOME, chunked=False, get_member=lambda user_id: None, fetch_member=fetch_member)]
    assert asyncio.run(user_app.shares_server(bot, types.SimpleNamespace(id=MEMBER))) is True
    assert asyncio.run(user_app.shares_server(bot, types.SimpleNamespace(id=STRANGER))) is False


def test_a_server_without_the_bot_has_no_settings_and_gets_none(storage):
    update_guild_settings(storage, HOME, {"blocked_commands": ["filter"], "bot_channels": [123]})
    bot = make_bot(storage)
    bot.command_gate = types.MethodType(main.GigaBot.command_gate, bot)
    bot.ensure_command_allowed = types.MethodType(main.GigaBot.ensure_command_allowed, bot)
    foreign = interaction(MEMBER, guild_id=FOREIGN)
    foreign.command = command("filter invert")
    assert asyncio.run(main.GigaBot.on_tree_interaction_check(bot, foreign)) is True
    assert asyncio.run(bot.ensure_command_allowed(foreign, "invert")) is True
    assert EmbedManager(bot).guild_color(user_app.home_guild(foreign)) is None
    assert _fetch_raw_guild_settings(storage, FOREIGN) is None  # no row was made for a server that never added the bot
    home = interaction(MEMBER, guild_id=HOME, bot_is_member=True)
    home.command = command("filter invert")
    assert asyncio.run(main.GigaBot.on_tree_interaction_check(bot, home)) is False  # its own server still has its say


def test_image_sources_away_from_the_bots_servers():
    tools = ImageToolkit(bot=types.SimpleNamespace(get_channel=lambda channel_id: None))
    fetched = []

    async def fetch(url):
        fetched.append(url)
        return b"picture"

    tools.fetch_bytes_from_url = fetch
    foreign = interaction(MEMBER, guild_id=FOREIGN)

    class Unreadable:
        def history(self, limit):
            raise AssertionError("a channel in a server without the bot is not searched")

    foreign.channel = Unreadable()
    try:  # nothing attached, nothing picked: say what to do instead of failing on a channel that cannot be read
        asyncio.run(tools.get_source_image(foreign))
    except ValueError as exc:
        assert str(exc) == NO_HISTORY_MESSAGE
    else:
        raise AssertionError("expected a refusal")

    # an image picked with Set Image Source is used through the link kept at the time
    link = "https://cdn.discordapp.com/attachments/1/2/cat.png"
    message = types.SimpleNamespace(
        attachments=[types.SimpleNamespace(url=link, filename="cat.png", content_type="image/png")], embeds=[],
    )
    assert tools.message_image_link(message) == (link, "cat.png")
    tools.sources.remember(MEMBER, 5, 77, url=link, filename="cat.png")
    assert asyncio.run(tools.get_source_image(foreign)) == (b"picture", "cat.png") and fetched == [link]
    assert tools.sources.get(MEMBER) is not None  # and stays picked for the next command

    tools.check_upload_size(foreign, b"x" * 100)  # the stand-in server's limit of 1 byte is not believed


def test_playlists_need_no_bot_channel_away_from_a_server(storage):
    from cogs.music import Music

    update_guild_settings(storage, HOME, {"bot_channels": [123]})
    cog = types.SimpleNamespace(bot=make_bot(storage))
    assert asyncio.run(Music.ensure_bot_channel_allowed(cog, interaction(MEMBER, guild_id=FOREIGN))) == (True, None)
    assert asyncio.run(Music.ensure_bot_channel_allowed(cog, interaction(MEMBER, private=True))) == (True, None)
    allowed, reason = asyncio.run(Music.ensure_bot_channel_allowed(cog, interaction(MEMBER, guild_id=HOME, bot_is_member=True)))
    assert allowed is False and "<#123>" in reason
    assert _fetch_raw_guild_settings(storage, FOREIGN) is None
