from __future__ import annotations

import time
from typing import Final

import discord
from discord import app_commands
from discord.ext import commands

HELP_COLOR = discord.Color.blurple()
HELP_VIEW_TIMEOUT_SECONDS = 180.0
MENTION_HELP_COOLDOWN_SECONDS = 5.0

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
    ("images", "🖼️", "Images & Memes", ["image"]),
    ("music", "🎵", "Music", ["music"]),
    (
        "moderation",
        "🛡️",
        "Moderation",
        [
            "kick", "ban", "tempban", "unban", "mute", "unmute", "warn", "warnings",
            "purge", "clean", "lock", "unlock", "prune", "role", "autorole",
            "nick", "poll", "say",
        ],
    ),
    ("reactionroles", "🎭", "Reaction Roles", ["reactionrole"]),
    ("settings", "⚙️", "Server Settings", ["settings"]),
    ("info", "ℹ️", "Info & Support", ["about", "userinfo", "feedback"]),
]


class HelpCategorySelect(discord.ui.Select["HelpView"]):
    def __init__(self, cog: "Help") -> None:
        options = [
            discord.SelectOption(label=label, emoji=emoji, value=key)
            for key, emoji, label, _names in CATEGORY_DEFS
        ]
        super().__init__(placeholder="Choose a category...", options=options)
        self.cog = cog

    async def callback(self, interaction: discord.Interaction) -> None:
        assert self.view is not None
        key = self.values[0]
        embed = self.cog.build_category_embed(key)
        view = HelpView(cog=self.cog, author_id=self.view.author_id, selected=key)
        await interaction.response.edit_message(embed=embed, view=view)
        view.message = self.view.message


class HelpHomeButton(discord.ui.Button["HelpView"]):
    def __init__(self, cog: "Help") -> None:
        super().__init__(label="Home", emoji="🏠", style=discord.ButtonStyle.secondary)
        self.cog = cog

    async def callback(self, interaction: discord.Interaction) -> None:
        assert self.view is not None
        embed = self.cog.build_home_embed()
        view = HelpView(cog=self.cog, author_id=self.view.author_id)
        await interaction.response.edit_message(embed=embed, view=view)
        view.message = self.view.message


class HelpView(discord.ui.View):
    def __init__(
        self,
        *,
        cog: "Help",
        author_id: int,
        selected: str | None = None,
        timeout: float = HELP_VIEW_TIMEOUT_SECONDS,
    ) -> None:
        super().__init__(timeout=timeout)
        self.cog = cog
        self.author_id = author_id
        self.message: discord.Message | None = None
        self.add_item(HelpCategorySelect(cog))
        if selected is not None:
            self.add_item(HelpHomeButton(cog))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                "This help menu isn't yours - run `/help` (or ping me) to get your own.",
                ephemeral=True,
            )
            return False
        return True

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
        return {key: (emoji, label, names) for key, emoji, label, names in CATEGORY_DEFS}

    def _category_command_count(self, names: list[str]) -> int:
        total = 0
        for name in names:
            node = self.bot.tree.get_command(name)
            if node is None:
                continue
            if isinstance(node, app_commands.Group):
                total += sum(1 for c in node.walk_commands() if isinstance(c, app_commands.Command))
            else:
                total += 1
        return total

    def build_home_embed(self) -> discord.Embed:
        lines = [
            f"{emoji} **{label}** - {self._category_command_count(names)} command(s)"
            for _key, emoji, label, names in CATEGORY_DEFS
        ]
        return self.bot.embeds.create(
            title="📖 GigaBot Help",
            description="Pick a category below to see what I can do.\n\n" + "\n".join(lines),
            color=HELP_COLOR,
            footer="Only you can see this menu.",
        )

    def build_category_embed(self, key: str) -> discord.Embed:
        emoji, label, names = self._category_lookup()[key]
        lines: list[str] = []
        for name in names:
            node = self.bot.tree.get_command(name)
            if node is None:
                continue
            if isinstance(node, app_commands.Group):
                for sub in node.walk_commands():
                    if isinstance(sub, app_commands.Command):
                        lines.append(f"**/{sub.qualified_name}** - {sub.description or 'No description.'}")
            else:
                lines.append(f"**/{name}** - {node.description or 'No description.'}")
        description = "\n".join(lines) if lines else "Nothing here yet."
        return self.bot.embeds.create(title=f"{emoji} {label}", description=description, color=HELP_COLOR)

    @app_commands.command(name="help", description="Show a categorized list of everything I can do.")
    async def help_command(self, interaction: discord.Interaction) -> None:
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
            await message.channel.send(
                f"📬 {message.author.mention} I sent you the help menu in your DMs!",
                delete_after=8,
            )
        except discord.Forbidden:
            sent = await message.channel.send(
                "*(Your DMs look closed, so here's the help menu - it'll clean itself up shortly.)*",
                embed=embed,
                view=view,
                delete_after=HELP_VIEW_TIMEOUT_SECONDS,
            )
            view.message = sent

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
