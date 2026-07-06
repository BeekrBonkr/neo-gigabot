from __future__ import annotations

from datetime import timedelta

import discord
from discord import app_commands
from discord.ext import commands

SUPPORT_FALLBACK_MESSAGE = "No support server has been configured for this bot yet."


def _format_duration(delta: timedelta) -> str:
    total_seconds = max(0, int(delta.total_seconds()))
    days, remainder = divmod(total_seconds, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes, seconds = divmod(remainder, 60)
    parts = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if minutes:
        parts.append(f"{minutes}m")
    if not parts or seconds:
        parts.append(f"{seconds}s")
    return " ".join(parts)


class Info(commands.Cog):
    """Public bot-info and feedback commands."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self._feedback_cooldowns: dict[int, float] = {}

    @app_commands.command(name="about", description="Show information about this bot.")
    async def about(self, interaction: discord.Interaction) -> None:
        uptime = discord.utils.utcnow() - self.bot.start_time
        total_members = sum(guild.member_count or 0 for guild in self.bot.guilds)

        fields = [
            self.bot.embeds.field("Servers", str(len(self.bot.guilds)), True),
            self.bot.embeds.field("Users", str(total_members), True),
            self.bot.embeds.field("Uptime", _format_duration(uptime), True),
            self.bot.embeds.field("Gateway Latency", f"{self.bot.latency * 1000:.0f} ms", True),
            self.bot.embeds.field("Library", f"discord.py {discord.__version__}", True),
        ]

        invite_url = discord.utils.oauth_url(
            self.bot.application_id,
            permissions=discord.Permissions(
                manage_roles=True,
                manage_channels=True,
                kick_members=True,
                ban_members=True,
                manage_nicknames=True,
                moderate_members=True,
                view_audit_log=True,
                send_messages=True,
                embed_links=True,
                attach_files=True,
                read_message_history=True,
                add_reactions=True,
                connect=True,
                speak=True,
                manage_messages=True,
                create_instant_invite=True,
            ),
            scopes=("bot", "applications.commands"),
        )
        fields.append(self.bot.embeds.field("Invite Me", f"[Click here]({invite_url})", True))

        support_invite = getattr(self.bot.config, "support_server_invite", "") or ""
        if support_invite:
            fields.append(self.bot.embeds.field("Support Server", f"[Join here]({support_invite})", True))

        embed = self.bot.embeds.create(
            title=f"About {self.bot.user.name}",
            description="Use `/help` for a categorized list of everything I can do.",
            color=discord.Color.blurple(),
            thumbnail_url=self.bot.user.display_avatar.url,
            fields=fields,
        )
        await self.bot.embeds.respond(interaction, embed=embed)

    @app_commands.command(name="feedback", description="Send feedback or a bug report to the bot's owner.")
    @app_commands.describe(message="Your feedback")
    @app_commands.checks.cooldown(1, 60.0)
    async def feedback(self, interaction: discord.Interaction, message: app_commands.Range[str, 1, 1000]) -> None:
        owner_id = getattr(self.bot.config, "owner_id", 0)
        owner = self.bot.get_user(owner_id) if owner_id else None
        if owner is None and owner_id:
            try:
                owner = await self.bot.fetch_user(owner_id)
            except (discord.NotFound, discord.HTTPException):
                owner = None

        if owner is None:
            await self.bot.embeds.error_interaction(
                interaction, "Feedback Unavailable", "No bot owner is configured to receive feedback.", ephemeral=True
            )
            return

        context = f" (from {interaction.guild.name})" if interaction.guild else " (from DMs)"
        embed = self.bot.embeds.create(
            title="📮 New Feedback",
            description=message,
            color=discord.Color.gold(),
            author_name=f"{interaction.user}{context}",
            author_icon_url=interaction.user.display_avatar.url,
            footer=f"User ID: {interaction.user.id}",
        )

        try:
            await owner.send(embed=embed)
        except discord.Forbidden:
            await self.bot.embeds.error_interaction(
                interaction, "Feedback Failed", "I couldn't deliver your feedback right now. Try again later.", ephemeral=True
            )
            return

        await self.bot.embeds.success_interaction(
            interaction, "Feedback Sent", "Thanks! Your feedback has been sent to the bot's owner.", ephemeral=True
        )

    # No local error handler here on purpose: a local handler that re-raises
    # for the non-cooldown case would stop the error from ever reaching the
    # global handler in main.py (CommandTree._call awaits the local handler
    # and only reaches the global one if that await doesn't itself raise) -
    # verified this empirically. The global handler already handles
    # CommandOnCooldown identically, so there's nothing this cog needs to add.


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Info(bot))
