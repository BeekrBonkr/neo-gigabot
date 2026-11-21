from __future__ import annotations

import logging

import discord
from discord.ext import commands

from utils.numbers import is_digits
from utils import tempvoice as store
from utils.modules import enabled_in
from utils.settings import get_guild_settings

LOGGER = logging.getLogger(__name__)
MAX_NAME_LENGTH = 100


def channel_name(template: str, member: discord.Member) -> str:
    name = (template or "{name}'s channel").replace("{name}", member.display_name).replace("{user}", member.name).strip()
    return (name or f"{member.display_name}'s channel")[:MAX_NAME_LENGTH]


class TempVoice(commands.Cog):
    """Join-to-create voice channels: joining the server's "create a channel"
    room gives the member their own voice channel, which is deleted once it is
    empty. The owner can rename it, set its limit and move people in Discord."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self._swept = False

    def _hub_id(self, guild_id: int) -> int | None:
        settings = get_guild_settings(self.bot.storage_path, guild_id)
        raw = str(settings.get("tempvoice_hub_channel_id") or "") if enabled_in(settings, "tempvoice") else ""
        return int(raw) if is_digits(raw) else None

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        """Channels that emptied while the bot was down would otherwise stay forever."""
        if self._swept:
            return
        self._swept = True
        for entry in store.all_channels(self.bot.storage_path):
            guild = self.bot.get_guild(entry["guild_id"])
            channel = guild.get_channel(entry["channel_id"]) if guild else None
            if channel is None:
                store.remove(self.bot.storage_path, entry["channel_id"])
            elif not channel.members:
                await self._delete(channel)

    async def _delete(self, channel: discord.abc.GuildChannel) -> None:
        try:
            await channel.delete(reason="Temporary voice channel is empty")
        except discord.NotFound:
            pass
        except discord.HTTPException as exc:
            LOGGER.warning("Could not delete temporary voice channel %s: %s", channel.id, exc)
            return  # keep the record so it is tried again
        store.remove(self.bot.storage_path, channel.id)

    @commands.Cog.listener()
    async def on_voice_state_update(self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState) -> None:
        if before.channel == after.channel:
            return
        try:
            left = before.channel
            if left is not None and not left.members and store.get(self.bot.storage_path, left.id) is not None:
                await self._delete(left)
            if after.channel is not None and not member.bot and after.channel.id == self._hub_id(member.guild.id):
                await self.create_for(member, after.channel)
        except Exception:  # noqa: BLE001
            LOGGER.exception("Temporary voice handling failed in guild %s", member.guild.id)

    async def create_for(self, member: discord.Member, hub: discord.VoiceChannel) -> None:
        guild = member.guild
        # Someone who already has a channel is sent back to it instead of getting a second one.
        for channel_id in store.for_owner(self.bot.storage_path, guild.id, member.id):
            existing = guild.get_channel(channel_id)
            if existing is not None:
                await self._move(member, existing)
                return
            store.remove(self.bot.storage_path, channel_id)
        settings = get_guild_settings(self.bot.storage_path, guild.id)
        # Start from the hub's own permissions. A channel created with overwrites of its own does
        # not take its category's, so without this a channel made under a staff-only or
        # verified-only hub would be open to everyone.
        overwrites = dict(getattr(hub, "overwrites", None) or {})
        overwrites[member] = discord.PermissionOverwrite(manage_channels=True, move_members=True, connect=True, view_channel=True)
        overwrites[guild.me] = discord.PermissionOverwrite(manage_channels=True, move_members=True, connect=True, view_channel=True)
        try:
            channel = await guild.create_voice_channel(
                channel_name(str(settings.get("tempvoice_name") or ""), member), category=hub.category, overwrites=overwrites,
                user_limit=int(settings.get("tempvoice_user_limit", 0) or 0), reason=f"Temporary voice channel for {member} ({member.id})",
            )
        except discord.HTTPException as exc:
            LOGGER.warning("Could not create a temporary voice channel in guild %s: %s", guild.id, exc)
            return
        store.add(self.bot.storage_path, channel.id, guild.id, member.id)
        if not await self._move(member, channel):
            await self._delete(channel)  # they left before they could be moved

    async def _move(self, member: discord.Member, channel: discord.abc.GuildChannel) -> bool:
        try:
            await member.move_to(channel, reason="Temporary voice channel")
            return True
        except discord.HTTPException:
            return False


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(TempVoice(bot))
