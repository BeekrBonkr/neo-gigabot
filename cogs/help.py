from __future__ import annotations

import contextvars
import datetime
import io
import time
from typing import Any, Callable, Final

import discord
from discord import app_commands
from discord.ext import commands

from utils import command_dates
from utils.checks import required_permissions
from utils.config import beta_notice
from utils.modules import disabled_module_for
from utils.settings import blocking_entry, get_guild_settings
from utils.user_app import home_guild
from utils.website import site_link, site_url

HELP_COLOR = discord.Color.blurple()
HELP_VIEW_TIMEOUT_SECONDS = 180.0
MENTION_HELP_COOLDOWN_SECONDS = 5.0
# A category is split into pages so no embed gets near Discord's 4096 character
# description limit (over it, the whole message is refused).
HELP_PAGE_LINES = 15
HELP_PAGE_CHARS = 2000

# Curated category -> command name list. This controls grouping, emoji, and display
# order; the actual descriptions shown are always pulled live from each command's
# own registered `description` (see build_category_embed), so this list only needs
# a touch-up when a command is added, renamed, or removed - not whenever a
# description or option changes. Names that resolve to an app_commands.Group (e.g.
# "playlist", "settings") get their subcommands expanded automatically.
CATEGORY_DEFS: Final[list[tuple[str, str, str, list[str]]]] = [
    (
        "fun",
        "😄",
        "Fun",
        [
            "ping", "server", "wiki", "weather", "reddit", "meme", "cursed", "gif",
            "cute", "beans", "catfact", "fact", "joke", "8ball", "coinflip", "roll",
            "ascii", "snipe", "hug", "kiss", "kill", "insult", "trivia", "tictactoe",
            "simon", "hotpotato",
        ],
    ),
    ("images", "🖼️", "Images & Memes", ["image", "filter", "animate", "memegen", "edit"]),
    ("audio", "🔊", "Audio", ["audio"]),
    ("music", "🎵", "Music", ["music"]),
    (
        "moderation",
        "🛡️",
        "Moderation",
        [
            "kick", "ban", "tempban", "unban", "mute", "unmute", "warn", "warnings",
            "purge", "clean", "lock", "unlock", "prune", "role", "autorole",
            "nick", "poll", "say", "modhistory", "note", "softban", "slowmode", "lockdown", "verification", "honeypot",
        ],
    ),
    ("community", "💬", "Community", ["suggest", "suggestion", "tag", "tags", "schedule", "stick", "stickembed", "stickslow", "stickstop", "stickstart", "stickremove", "getstickies"]),
    ("reactionroles", "🎭", "Reaction Roles & Role Menus", ["reactionrole", "rolemenu"]),
    ("settings", "⚙️", "Server Settings", ["settings"]),
    ("info", "ℹ️", "Info & Support", ["about", "whatsnew", "userinfo", "feedback"]),
]
# Not a category of its own: every command from the categories above that was
# added recently, newest first. It is only offered while there are some.
NEW_KEY, NEW_EMOJI, NEW_LABEL = "new", "🆕", "What's New"


# Who the menu is being built for: a test of whether that person can use a
# command where they are, or None to list everything. It is set where a help
# menu is opened or one of its controls is used, and read wherever commands
# are listed, so every page and every count agree.
_usable: contextvars.ContextVar[Callable[[app_commands.Command], bool] | None] = contextvars.ContextVar("help_usable", default=None)


def usable_by(bot: commands.Bot, guild: discord.Guild | None, member: Any) -> Callable[[app_commands.Command], bool] | None:
    """A test for "this person can use this command in this server": they hold
    the permissions it needs, and the server has not disabled it or turned its
    module off. None (no filtering) outside a server that has the bot, where
    there are no server permissions or settings to go by."""
    permissions = getattr(member, "guild_permissions", None)
    if guild is None or permissions is None:
        return None
    settings = get_guild_settings(bot.storage_path, guild.id)
    blocked = settings.get("blocked_commands")

    def usable(command: app_commands.Command) -> bool:
        name = command.qualified_name
        if name != "settings" and (blocking_entry(name, blocked) is not None or disabled_module_for(name, settings) is not None):
            return False
        if permissions.administrator:
            return True
        root = command.root_parent or command
        needed = getattr(root, "default_permissions", None)
        if needed is not None and not permissions >= needed:
            return False
        return all(getattr(permissions, permission, False) for permission in required_permissions(command))

    return usable


class HelpCategorySelect(discord.ui.Select["HelpView"]):
    def __init__(self, cog: "Help") -> None:
        options = [
            discord.SelectOption(label=label, emoji=emoji, value=key)
            for key, emoji, label, names in CATEGORY_DEFS
            if _usable.get() is None or cog._commands_in(names)  # a category with nothing this person can use is left out
        ]
        if cog.new_commands():
            options.insert(0, discord.SelectOption(label=NEW_LABEL, emoji=NEW_EMOJI, value=NEW_KEY))
        super().__init__(placeholder="Choose a category...", options=options)
        self.cog = cog

    async def callback(self, interaction: discord.Interaction) -> None:
        assert self.view is not None
        await self.view.show(interaction, self.values[0], 0)


class HelpHomeButton(discord.ui.Button["HelpView"]):
    def __init__(self, cog: "Help") -> None:
        super().__init__(label="Home", emoji="🏠", style=discord.ButtonStyle.secondary)
        self.cog = cog

    async def callback(self, interaction: discord.Interaction) -> None:
        assert self.view is not None
        await self.view.show(interaction, None, 0)


class HelpPageButton(discord.ui.Button["HelpView"]):
    def __init__(self, cog: "Help", *, label: str, page: int, disabled: bool) -> None:
        super().__init__(label=label, style=discord.ButtonStyle.primary, disabled=disabled)
        self.cog = cog
        self.page = page

    async def callback(self, interaction: discord.Interaction) -> None:
        assert self.view is not None
        await self.view.show(interaction, self.view.selected, self.page)


class HelpView(discord.ui.View):
    def __init__(
        self,
        *,
        cog: "Help",
        author_id: int,
        selected: str | None = None,
        page: int = 0,
        timeout: float = HELP_VIEW_TIMEOUT_SECONDS,
    ) -> None:
        super().__init__(timeout=timeout)
        self.cog = cog
        self.author_id = author_id
        self.selected = selected
        self.message: discord.Message | None = None
        self.add_item(HelpCategorySelect(cog))
        if selected is not None:
            pages = len(cog.category_pages(selected))
            if pages > 1:
                self.add_item(HelpPageButton(cog, label="◀ Previous", page=page - 1, disabled=page <= 0))
                self.add_item(HelpPageButton(cog, label="Next ▶", page=page + 1, disabled=page >= pages - 1))
            self.add_item(HelpHomeButton(cog))

    async def show(self, interaction: discord.Interaction, key: str | None, page: int) -> None:
        """Swap the message over to a category page (or home when key is None)."""
        embed = self.cog.build_home_embed() if key is None else self.cog.build_category_embed(key, page)
        view = HelpView(cog=self.cog, author_id=self.author_id, selected=key, page=page)
        await interaction.response.edit_message(embed=embed, view=view)
        view.message = self.message
        self.stop()  # the old view must not time out and disable the new one's buttons

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                "This help menu isn't yours - run `/help` (or ping me) to get your own.",
                ephemeral=True,
            )
            return False
        # the page about to be shown is built for whoever pressed, as they stand now
        bot = getattr(self.cog, "bot", None)
        _usable.set(usable_by(bot, home_guild(interaction), interaction.user) if bot is not None else None)
        return True

    async def on_timeout(self) -> None:
        for item in self.children:
            item.disabled = True
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass


WHATS_NEW_SUMMARY_LINES = 10


class WhatsNewButton(discord.ui.Button["WhatsNewView"]):
    def __init__(self, *, label: str, page: int, disabled: bool) -> None:
        super().__init__(label=label, style=discord.ButtonStyle.primary, disabled=disabled)
        self.page = page

    async def callback(self, interaction: discord.Interaction) -> None:
        assert self.view is not None
        await self.view.show(interaction, self.page)


class WhatsNewView(discord.ui.View):
    """/whatsnew: a summary, then one page per new command with its sample.
    Only the person who ran it sees it, so the buttons need no owner check."""

    def __init__(self, *, cog: "Help", page: int = 0, timeout: float = HELP_VIEW_TIMEOUT_SECONDS) -> None:
        super().__init__(timeout=timeout)
        self.cog = cog
        self.message: discord.Message | None = None
        last = len(cog.new_commands())
        self.add_item(WhatsNewButton(label="◀ Previous", page=page - 1, disabled=page <= 0))
        self.add_item(WhatsNewButton(label="Next ▶" if page else "Browse them ▶", page=page + 1, disabled=page >= last))

    async def show(self, interaction: discord.Interaction, page: int) -> None:
        await interaction.response.defer()  # a sample that has not been made yet takes a moment
        embed, files = await self.cog.build_whats_new(page)
        view = WhatsNewView(cog=self.cog, page=page)
        await interaction.edit_original_response(embed=embed, attachments=files, view=view)
        view.message = self.message
        self.stop()

    async def on_timeout(self) -> None:
        for item in self.children:
            item.disabled = True
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass


class Help(commands.Cog):
    """A curated, categorized help menu - Discord's own slash-command list gets
    unwieldy fast once a bot has this many commands."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self._mention_cooldowns: dict[int, float] = {}

    def _category_lookup(self) -> dict[str, tuple[str, str, list[str]]]:
        lookup = {key: (emoji, label, names) for key, emoji, label, names in CATEGORY_DEFS}
        lookup[NEW_KEY] = (NEW_EMOJI, NEW_LABEL, [])
        return lookup

    def _commands_in(self, names: list[str]) -> list[app_commands.Command]:
        found: list[app_commands.Command] = []
        for name in names:
            node = self.bot.tree.get_command(name)
            if isinstance(node, app_commands.Group):
                found.extend(sub for sub in node.walk_commands() if isinstance(sub, app_commands.Command))
            elif node is not None:
                found.append(node)
        usable = _usable.get()
        return found if usable is None else [command for command in found if usable(command)]

    def new_commands(self) -> list[app_commands.Command]:
        """Recently added commands from the help categories, newest first."""
        listed = [command for _key, _emoji, _label, names in CATEGORY_DEFS for command in self._commands_in(names)]
        fresh = [command for command in listed if command_dates.is_new(command.qualified_name)]
        return sorted(fresh, key=lambda command: (command_dates.added_on(command.qualified_name), command.qualified_name), reverse=True)

    @staticmethod
    def _line(command: app_commands.Command) -> str:
        marker = f" {NEW_EMOJI}" if command_dates.is_new(command.qualified_name) else ""
        return f"**/{command.qualified_name}**{marker} - {command.description or 'No description.'}"

    def _category_command_count(self, names: list[str]) -> int:
        return len(self._commands_in(names))

    def build_home_embed(self) -> discord.Embed:
        counts = [(emoji, label, self._category_command_count(names)) for _key, emoji, label, names in CATEGORY_DEFS]
        lines = [f"{emoji} **{label}** - {count} command(s)" for emoji, label, count in counts if count]
        hidden = _usable.get() is not None and any(count == 0 for _emoji, _label, count in counts)
        fresh = len(self.new_commands())
        if fresh:
            lines.insert(0, f"{NEW_EMOJI} **{NEW_LABEL}** - {fresh} command(s) added in the last month")
        return self.bot.embeds.create(
            title="📖 GigaBot Help",
            description=(f"{beta_notice(self.bot.config)}\n\n" if beta_notice(self.bot.config) else "")
            + ("Pick a category below to see what you can use in this server.\n\n" if _usable.get() is not None
               else "Pick a category below to see what I can do.\n\n")
            + "\n".join(lines)
            + ("\n\n*Commands you don't have the permissions for, and ones this server has turned off, are left out.*" if hidden else "")
            + (f"\n\n🌐 **Website** - {site_link(self.bot, 'open it')} for the web music player, playlists, the full command list, "
               "and a settings dashboard for admins." if site_url(self.bot) else ""),
            color=HELP_COLOR,
            footer="Only you can see this menu.",
        )

    def _category_lines(self, key: str) -> list[str]:
        if key == NEW_KEY:
            return [self._line(command) for command in self.new_commands()]
        _emoji, _label, names = self._category_lookup()[key]
        return [self._line(command) for command in self._commands_in(names)]

    def category_pages(self, key: str) -> list[str]:
        """The category's command list, cut into embed-sized pages."""
        pages: list[list[str]] = [[]]
        for line in self._category_lines(key):
            current = pages[-1]
            if current and (len(current) >= HELP_PAGE_LINES or sum(map(len, current)) + len(current) + len(line) > HELP_PAGE_CHARS):
                pages.append(current := [])
            current.append(line)
        return ["\n".join(page) for page in pages]

    def build_category_embed(self, key: str, page: int = 0) -> discord.Embed:
        emoji, label, _names = self._category_lookup()[key]
        pages = self.category_pages(key)
        page = max(0, min(page, len(pages) - 1))
        return self.bot.embeds.create(
            title=f"{emoji} {label}",
            description=pages[page] or "Nothing here yet.",
            color=HELP_COLOR,
            footer=f"Page {page + 1} of {len(pages)}" if len(pages) > 1 else None,
        )

    async def build_whats_new(self, page: int) -> tuple[discord.Embed, list[discord.File]]:
        """Page 0 sums up what is new; each page after it is one command,
        newest first, with a sample of what it makes when it has one."""
        fresh = self.new_commands()
        if not fresh:
            return self.bot.embeds.create(title=f"{NEW_EMOJI} {NEW_LABEL}", description="Nothing new in the last month.", color=HELP_COLOR), []
        page = max(0, min(page, len(fresh)))
        if page == 0:
            lines = [self._line(command).replace(f" {NEW_EMOJI}", "") for command in fresh[:WHATS_NEW_SUMMARY_LINES]]
            if len(fresh) > len(lines):
                lines.append(f"...and {len(fresh) - len(lines)} more.")
            site = f"\n\n🌐 {site_link(self.bot, 'See them all with examples', '/whatsnew')}" if site_url(self.bot) else ""
            return self.bot.embeds.create(
                title=f"{NEW_EMOJI} {NEW_LABEL}",
                description=f"**{len(fresh)}** command(s) added in the last month, newest first.\n\n" + "\n".join(lines) + site,
                color=HELP_COLOR,
            ), []
        command = fresh[page - 1]
        added = datetime.date.fromisoformat(command_dates.added_on(command.qualified_name))
        embed = self.bot.embeds.create(
            title=f"{NEW_EMOJI} /{command.qualified_name}",
            description=f"{command.description or 'No description.'}\n\nAdded {added.strftime('%B')} {added.day}, {added.year}.",
            color=HELP_COLOR,
            footer=f"{page} of {len(fresh)} · Only you can see this.",
        )
        sample = await self.bot.samples.get(command.qualified_name)
        if sample is None:
            return embed, []
        data, filename = sample
        embed.set_image(url=f"attachment://{filename}")
        return embed, [discord.File(io.BytesIO(data), filename=filename)]

    @app_commands.command(name="whatsnew", description="See the commands added in the last month, with examples.")
    @app_commands.checks.cooldown(1, 5.0)
    async def whats_new(self, interaction: discord.Interaction) -> None:
        embed, files = await self.build_whats_new(0)
        view = WhatsNewView(cog=self)
        await interaction.response.send_message(embed=embed, files=files, view=view, ephemeral=True)
        view.message = await interaction.original_response()

    @app_commands.command(name="help", description="Show a categorized list of everything I can do.")
    async def help_command(self, interaction: discord.Interaction) -> None:
        _usable.set(usable_by(self.bot, home_guild(interaction), interaction.user))
        view = HelpView(cog=self, author_id=interaction.user.id)
        embed = self.build_home_embed()
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)
        view.message = await interaction.original_response()

    def _mention_on_cooldown(self, user_id: int) -> bool:
        now = time.monotonic()
        last = self._mention_cooldowns.get(user_id, 0.0)
        if now - last < MENTION_HELP_COOLDOWN_SECONDS:
            return True
        # Expired entries are otherwise never removed, so sweep them once the
        # dict has accumulated enough users to matter.
        if len(self._mention_cooldowns) > 512:
            self._mention_cooldowns = {
                uid: ts for uid, ts in self._mention_cooldowns.items() if now - ts < MENTION_HELP_COOLDOWN_SECONDS
            }
        self._mention_cooldowns[user_id] = now
        return False

    async def _send_help_via_message(self, message: discord.Message) -> None:
        if self._mention_on_cooldown(message.author.id):
            return

        _usable.set(None)  # this menu is read in DMs, away from any one server: it lists everything
        embed = self.build_home_embed()
        view = HelpView(cog=self, author_id=message.author.id)

        # Already in a DM - nowhere else to send it, just reply here directly.
        if message.guild is None:
            sent = await message.channel.send(embed=embed, view=view)
            view.message = sent
            return

        # In a server: DM it so it doesn't clutter the channel, with a
        # self-deleting notice so the pinger still gets visible confirmation.
        try:
            dm_channel = await message.author.create_dm()
            sent = await dm_channel.send(embed=embed, view=view)
            view.message = sent
        except discord.HTTPException:
            # DMs closed (Forbidden) or some other delivery failure: fall back to
            # the channel. The channel itself may also be unwritable (the bot was
            # mentioned somewhere it lacks Send Messages), which is nothing to
            # trace - there's simply nowhere left to answer.
            try:
                sent = await message.channel.send(
                    "*(Your DMs look closed, so here's the help menu - it'll clean itself up shortly.)*",
                    embed=embed,
                    view=view,
                    delete_after=HELP_VIEW_TIMEOUT_SECONDS,
                )
            except discord.HTTPException:
                return
            view.message = sent
            return

        try:
            await message.channel.send(
                f"📬 {message.author.mention} I sent you the help menu in your DMs!",
                delete_after=8,
            )
        except discord.HTTPException:
            # The DM went through; a missing confirmation notice is harmless.
            pass

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot or self.bot.user is None:
            return

        # Only a *bare* mention (nothing else typed) triggers help - mentioning the
        # bot conversationally alongside other text shouldn't hijack the message.
        content = message.content.strip()
        if content not in (f"<@{self.bot.user.id}>", f"<@!{self.bot.user.id}>"):
            return

        await self._send_help_via_message(message)

    @commands.command(name="help")
    async def help_text_command(self, ctx: commands.Context) -> None:
        await self._send_help_via_message(ctx.message)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Help(bot))
