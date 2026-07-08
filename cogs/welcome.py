from __future__ import annotations

import discord
from discord.ext import commands

from utils.settings import get_guild_settings, get_welcome_delete_after


def _substitute(template: str, member: discord.Member) -> str:
    return (
        template.replace("{mention}", member.mention)
        .replace("{server}", member.guild.name)
        .replace("{user}", str(member))
    )


class Welcome(commands.Cog):
    """Sends the configured join/leave greeting to the configured channel(s).

    Deliberately separate from cogs/logging.py's own on_member_join/remove
    listeners - that cog is a private audit trail for server admins, this one
    is a public greeting. They share a trigger event, not a purpose.
    """

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    # The greeting template is admin-provided text; only allow user pings so a
    # template containing @everyone/@here or a role mention can't mass-ping on
    # every single join/leave.
    _ALLOWED_MENTIONS = discord.AllowedMentions(users=True, everyone=False, roles=False)

    async def _send_to_configured_channels(self, guild: discord.Guild, channel_ids: list, content: str) -> None:
        # 0 means keep the greeting; anything else auto-deletes it after that
        # many seconds so busy servers don't accumulate join/leave noise.
        delete_after = get_welcome_delete_after(self.bot.storage_path, guild.id) or None
        for raw_id in channel_ids:
            try:
                channel_id = int(raw_id)
            except (TypeError, ValueError):
                continue
            channel = guild.get_channel(channel_id)
            if isinstance(channel, discord.TextChannel):
                try:
                    await channel.send(content, allowed_mentions=self._ALLOWED_MENTIONS, delete_after=delete_after)
                except (discord.Forbidden, discord.HTTPException):
                    pass

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member) -> None:
        if member.bot:
            return
        settings = get_guild_settings(self.bot.storage_path, member.guild.id)
        channel_ids = settings.get("welcome_channel_ids", []) or []
        message = settings.get("join_message") or ""
        if not channel_ids or not message:
            return
        await self._send_to_configured_channels(member.guild, channel_ids, _substitute(message, member))

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member) -> None:
        if member.bot:
            return
        settings = get_guild_settings(self.bot.storage_path, member.guild.id)
        # Leave messages go to their own channels when configured, otherwise
        # they share the welcome channel(s) as before.
        channel_ids = settings.get("leave_channel_ids") or settings.get("welcome_channel_ids") or []
        message = settings.get("leave_message") or ""
        if not channel_ids or not message:
            return
        await self._send_to_configured_channels(member.guild, channel_ids, _substitute(message, member))


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Welcome(bot))
