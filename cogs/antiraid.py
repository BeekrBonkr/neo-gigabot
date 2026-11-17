from __future__ import annotations

import logging
import time
from collections import deque
from datetime import timedelta

import discord
from discord import app_commands
from discord.ext import commands

from utils.cases import add_case
from utils.modules import enabled_in
from utils.settings import get_guild_settings, get_logging_channel_id, get_mod_log_channel_id

LOGGER = logging.getLogger(__name__)


class JoinTracker:
    """Join times and lockdown state per server. In memory on purpose: a
    lockdown is a response to something happening right now, and a restart
    ending one early is harmless."""

    def __init__(self) -> None:
        self._joins: dict[int, deque[float]] = {}
        self._locked_until: dict[int, float] = {}

    def record_join(self, guild_id: int, limit: int, window: float, now: float | None = None) -> bool:
        """Note a join; True when it is the `limit`-th within `window` seconds."""
        now = time.monotonic() if now is None else now
        joins = self._joins.setdefault(guild_id, deque())
        while joins and joins[0] <= now - window:
            joins.popleft()
        joins.append(now)
        return len(joins) >= limit

    def lock(self, guild_id: int, minutes: float, now: float | None = None) -> None:
        self._locked_until[guild_id] = (time.monotonic() if now is None else now) + minutes * 60
        self._joins.pop(guild_id, None)

    def unlock(self, guild_id: int) -> bool:
        return self._locked_until.pop(guild_id, None) is not None

    def remaining(self, guild_id: int, now: float | None = None) -> float:
        """Seconds of lockdown left, 0 when the server isn't locked down."""
        until = self._locked_until.get(guild_id)
        if until is None:
            return 0.0
        left = until - (time.monotonic() if now is None else now)
        if left <= 0:
            del self._locked_until[guild_id]
            return 0.0
        return left


def account_too_new(created_at, min_days: int, now=None) -> bool:
    if min_days <= 0:
        return False
    return (now or discord.utils.utcnow()) - created_at < timedelta(days=min_days)


class AntiRaid(commands.Cog):
    """Kicks accounts younger than the server allows, and locks the server down
    (kicking new joiners for a while) when too many accounts join at once."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.tracker = JoinTracker()

    async def _alert(self, guild: discord.Guild, title: str, description: str) -> None:
        """Tell the moderators: in the mod-log channel, else the activity-log channel."""
        channel_id = get_mod_log_channel_id(self.bot.storage_path, guild.id) or get_logging_channel_id(self.bot.storage_path, guild.id)
        channel = guild.get_channel(channel_id) if channel_id else None
        if channel is None:
            return
        try:
            await channel.send(embed=self.bot.embeds.warning_embed(title, description))
        except discord.HTTPException:
            pass

    async def _turn_away(self, member: discord.Member, why: str, dm: str) -> None:
        try:
            await member.send(embed=self.bot.embeds.warning_embed(f"You were removed from {member.guild.name}", dm))
        except discord.HTTPException:
            pass
        try:
            await member.kick(reason=why)
        except discord.HTTPException as exc:
            LOGGER.warning("Anti-raid could not kick %s in guild %s: %s", member.id, member.guild.id, exc)
            return
        add_case(self.bot.storage_path, member.guild.id, member.id, self.bot.user.id, "Anti-Raid Kick", why)

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member) -> None:
        if member.bot:
            return
        guild = member.guild
        settings = get_guild_settings(self.bot.storage_path, guild.id)
        if not enabled_in(settings, "antiraid"):
            return
        min_days = int(settings.get("antiraid_min_account_age_days", 0) or 0)
        if account_too_new(member.created_at, min_days):
            await self._turn_away(
                member, f"Account younger than {min_days} day(s)",
                f"This server only accepts Discord accounts that are at least {min_days} day(s) old. You're welcome back once yours is.",
            )
            return
        if self.tracker.remaining(guild.id) > 0:
            await self._turn_away(member, "Joined during a lockdown", "The server is temporarily not accepting new members. Please try again later.")
            return
        if not settings.get("antiraid_enabled"):
            return
        limit, window = int(settings.get("antiraid_join_limit", 10) or 10), int(settings.get("antiraid_window_seconds", 30) or 30)
        if self.tracker.record_join(guild.id, limit, window):
            minutes = int(settings.get("antiraid_lockdown_minutes", 10) or 10)
            self.tracker.lock(guild.id, minutes)
            LOGGER.warning("Anti-raid lockdown started in guild %s (%s joins in %ss)", guild.id, limit, window)
            await self._alert(
                guild, "🛡️ Lockdown Started",
                f"{limit} accounts joined within {window} seconds. New members will be kicked for the next **{minutes} minute(s)**.\n"
                "Use `/lockdown off` to end it early. Members who joined before the lockdown were not touched.",
            )

    lockdown = app_commands.Group(
        name="lockdown", description="Stop new members from joining for a while.", guild_only=True,
        default_permissions=discord.Permissions(kick_members=True),
    )

    @lockdown.command(name="on", description="Start a lockdown: new members are kicked until it ends.")
    @app_commands.describe(minutes="How long the lockdown lasts (default 10)")
    @app_commands.checks.has_permissions(kick_members=True)
    @app_commands.checks.bot_has_permissions(kick_members=True)
    async def lockdown_on(self, interaction: discord.Interaction, minutes: app_commands.Range[int, 1, 1440] = 10) -> None:
        self.tracker.lock(interaction.guild.id, minutes)
        await self.bot.embeds.success_interaction(interaction, "Lockdown Started", f"New members will be kicked for the next **{minutes} minute(s)**. End it with `/lockdown off`.")
        await self._alert(interaction.guild, "🛡️ Lockdown Started", f"Started by {interaction.user.mention} for {minutes} minute(s).")

    @lockdown.command(name="off", description="End the lockdown now.")
    @app_commands.checks.has_permissions(kick_members=True)
    async def lockdown_off(self, interaction: discord.Interaction) -> None:
        if not self.tracker.unlock(interaction.guild.id):
            await self.bot.embeds.info_interaction(interaction, "No Lockdown", "The server isn't locked down.", ephemeral=True)
            return
        await self.bot.embeds.success_interaction(interaction, "Lockdown Ended", "New members can join again.")
        await self._alert(interaction.guild, "🛡️ Lockdown Ended", f"Ended by {interaction.user.mention}.")

    @lockdown.command(name="status", description="Show whether the server is locked down.")
    @app_commands.checks.has_permissions(kick_members=True)
    async def lockdown_status(self, interaction: discord.Interaction) -> None:
        left = self.tracker.remaining(interaction.guild.id)
        text = f"Locked down for another **{max(1, round(left / 60))} minute(s)**." if left else "The server isn't locked down."
        await self.bot.embeds.info_interaction(interaction, "Lockdown", text, ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(AntiRaid(bot))
