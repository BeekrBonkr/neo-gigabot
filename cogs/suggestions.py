from __future__ import annotations

import logging

import discord
from discord import app_commands
from discord.ext import commands

from utils import suggestions as store
from utils.settings import get_guild_settings, update_guild_settings

LOGGER = logging.getLogger(__name__)
UP_ID, DOWN_ID = "suggestions:up", "suggestions:down"
STATUS_STYLE = {"open": ("💡", discord.Color.blurple()), "approved": ("✅", discord.Color.green()), "denied": ("❌", discord.Color.red())}


def build_embed(suggestion: dict, author: discord.abc.User | None) -> discord.Embed:
    emoji, color = STATUS_STYLE[suggestion["status"]]
    up, down = store.tally(suggestion)
    embed = discord.Embed(title=f"{emoji} Suggestion #{suggestion['number']}", description=suggestion["text"], color=color)
    if author is not None:
        embed.set_author(name=str(author), icon_url=author.display_avatar.url)
    embed.add_field(name="Votes", value=f"👍 {up}   👎 {down}", inline=True)
    if suggestion["status"] != "open":
        decision = suggestion["status"].capitalize() + (f" by <@{suggestion['decided_by']}>" if suggestion.get("decided_by") else "")
        embed.add_field(name="Decision", value=decision + (f"\n{suggestion['decision_reason']}" if suggestion["decision_reason"] else ""), inline=False)
    else:
        embed.set_footer(text="Vote with the buttons. Press yours again to take it back.")
    return embed


class SuggestionView(discord.ui.View):
    """The vote buttons under every suggestion. Fixed custom IDs and no
    timeout, so they keep working after a restart; the message tells them
    which suggestion they belong to."""

    def __init__(self, cog: "Suggestions", *, closed: bool = False) -> None:
        super().__init__(timeout=None)
        self.cog = cog
        if closed:
            for item in self.children:
                item.disabled = True

    @discord.ui.button(emoji="👍", style=discord.ButtonStyle.secondary, custom_id=UP_ID)
    async def up(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self.cog.handle_vote(interaction, 1)

    @discord.ui.button(emoji="👎", style=discord.ButtonStyle.secondary, custom_id=DOWN_ID)
    async def down(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self.cog.handle_vote(interaction, -1)


class Suggestions(commands.Cog):
    """Members post ideas with /suggest, everyone votes with buttons, and
    staff approve or deny them."""

    suggestion = app_commands.Group(
        name="suggestion", description="Set up and manage member suggestions.", guild_only=True,
        default_permissions=discord.Permissions(manage_messages=True),
    )

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    async def cog_load(self) -> None:
        self.bot.add_view(SuggestionView(self))

    def _channel(self, guild: discord.Guild) -> discord.TextChannel | None:
        raw = str(get_guild_settings(self.bot.storage_path, guild.id).get("suggestions_channel_id") or "")
        channel = guild.get_channel(int(raw)) if raw.isdigit() else None
        return channel if isinstance(channel, discord.TextChannel) else None

    async def handle_vote(self, interaction: discord.Interaction, value: int) -> None:
        suggestion, outcome = store.vote(self.bot.storage_path, interaction.message.id, interaction.user.id, value)
        if suggestion is None:
            await interaction.response.send_message("This suggestion is no longer on record.", ephemeral=True)
            return
        if outcome == "closed":
            await interaction.response.send_message(f"Voting is closed: this suggestion was {suggestion['status']}.", ephemeral=True)
            return
        author = self.bot.get_user(suggestion["author_id"])
        await interaction.response.edit_message(embed=build_embed(suggestion, author))

    @app_commands.command(name="suggest", description="Suggest something for this server. Everyone can vote on it.")
    @app_commands.describe(idea="Your suggestion")
    @app_commands.checks.cooldown(1, 60.0)
    @app_commands.guild_only()
    async def suggest(self, interaction: discord.Interaction, idea: app_commands.Range[str, 5, store.MAX_SUGGESTION_LENGTH]) -> None:
        channel = self._channel(interaction.guild)
        if channel is None:
            await self.bot.embeds.error_interaction(
                interaction, "Suggestions Are Off", "This server hasn't set a suggestions channel. An admin can pick one with `/suggestion channel`.", ephemeral=True
            )
            return
        suggestion = store.create(self.bot.storage_path, interaction.guild.id, interaction.user.id, idea)
        try:
            message = await channel.send(embed=build_embed(suggestion, interaction.user), view=SuggestionView(self))
        except discord.HTTPException:
            store.delete(self.bot.storage_path, suggestion["id"])
            await self.bot.embeds.error_interaction(interaction, "Could Not Post", f"I can't send messages in {channel.mention}.", ephemeral=True)
            return
        store.set_message(self.bot.storage_path, suggestion["id"], channel.id, message.id)
        await self.bot.embeds.success_interaction(interaction, "Suggestion Posted", f"Suggestion #{suggestion['number']} is up in {channel.mention}: {message.jump_url}", ephemeral=True)

    @suggestion.command(name="channel", description="Choose the channel suggestions are posted in, or turn them off.")
    @app_commands.describe(channel="Where suggestions go (leave empty to turn suggestions off)")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def set_channel(self, interaction: discord.Interaction, channel: discord.TextChannel | None = None) -> None:
        update_guild_settings(self.bot.storage_path, interaction.guild.id, {"suggestions_channel_id": str(channel.id) if channel else ""})
        if channel is None:
            await self.bot.embeds.success_interaction(interaction, "Suggestions Off", "`/suggest` is turned off. Existing suggestions stay where they are.", ephemeral=True)
        else:
            await self.bot.embeds.success_interaction(
                interaction, "Suggestions Channel Set",
                f"`/suggest` now posts in {channel.mention}. Consider making it read-only for members so it only holds suggestions.", ephemeral=True,
            )

    async def _decide(self, interaction: discord.Interaction, number: int, status: str, reason: str | None) -> None:
        suggestion = store.decide(self.bot.storage_path, interaction.guild.id, number, status, interaction.user.id, reason or "")
        if suggestion is None:
            await self.bot.embeds.error_interaction(interaction, "Not Found", f"There is no suggestion #{number} in this server.", ephemeral=True)
            return
        updated = False
        channel = interaction.guild.get_channel(suggestion["channel_id"])
        if channel is not None and suggestion["message_id"]:
            try:
                message = await channel.fetch_message(suggestion["message_id"])
                await message.edit(embed=build_embed(suggestion, self.bot.get_user(suggestion["author_id"])), view=SuggestionView(self, closed=True))
                updated = True
            except discord.HTTPException:
                pass
        author = self.bot.get_user(suggestion["author_id"])
        if author is not None:
            try:
                await author.send(embed=self.bot.embeds.info_embed(
                    f"Your suggestion was {status}",
                    f"Suggestion #{number} in **{interaction.guild.name}**:\n> {suggestion['text'][:300]}" + (f"\n\n{reason}" if reason else ""),
                ))
            except discord.HTTPException:
                pass
        await self.bot.embeds.success_interaction(
            interaction, f"Suggestion {status.capitalize()}",
            f"Suggestion #{number} was {status}." + ("" if updated else " I couldn't update its message (it may have been deleted)."), ephemeral=True,
        )

    @suggestion.command(name="approve", description="Approve a suggestion and close its voting.")
    @app_commands.describe(number="The suggestion's number", reason="Shown on the suggestion and sent to its author")
    @app_commands.checks.has_permissions(manage_messages=True)
    async def approve(self, interaction: discord.Interaction, number: int, reason: str | None = None) -> None:
        await self._decide(interaction, number, "approved", reason)

    @suggestion.command(name="deny", description="Deny a suggestion and close its voting.")
    @app_commands.describe(number="The suggestion's number", reason="Shown on the suggestion and sent to its author")
    @app_commands.checks.has_permissions(manage_messages=True)
    async def deny(self, interaction: discord.Interaction, number: int, reason: str | None = None) -> None:
        await self._decide(interaction, number, "denied", reason)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Suggestions(bot))
