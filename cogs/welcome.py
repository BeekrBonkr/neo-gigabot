from __future__ import annotations

import asyncio
import io
import logging

import discord
from discord.ext import commands

from utils.modules import enabled_in
from utils.settings import get_embed_color_value, get_guild_settings, get_welcome_delete_after, parse_hex_color
from utils.welcome_card import render_card

LOGGER = logging.getLogger(__name__)


def _substitute(template: str, member: discord.Member) -> str:
    """Fill in a greeting. {mention} pings them, {user} is their username,
    {name} their display name, {server} the server and {count} how many
    members it has now."""
    return (
        template.replace("{mention}", member.mention)
        .replace("{server}", member.guild.name)
        .replace("{user}", str(member))
        .replace("{name}", member.display_name)
        .replace("{count}", f"{member.guild.member_count or 0:,}")
    )


class Welcome(commands.Cog):
    """Sends the configured join/leave greeting to the configured channel(s).

    A join greeting is plain text, an embed, or a generated image card
    (welcome_style), and can also be sent to the new member as a DM.

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

    async def _send_to_configured_channels(self, guild: discord.Guild, channel_ids: list, content: str | None, *,
                                           embed: discord.Embed | None = None, image: bytes | None = None) -> None:
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
                kwargs = {}
                if embed is not None:
                    kwargs["embed"] = embed
                if image is not None:  # a File can only be sent once, so each channel gets its own
                    kwargs["file"] = discord.File(io.BytesIO(image), filename="welcome.png")
                try:
                    await channel.send(content, allowed_mentions=self._ALLOWED_MENTIONS, delete_after=delete_after, **kwargs)
                except (discord.Forbidden, discord.HTTPException):
                    pass

    def _accent(self, settings: dict, guild_id: int) -> int | None:
        return parse_hex_color(str(settings.get("welcome_embed_color") or "")) or get_embed_color_value(self.bot.storage_path, guild_id)

    def build_embed(self, member: discord.Member, settings: dict) -> discord.Embed:
        accent = self._accent(settings, member.guild.id)
        embed = discord.Embed(
            title=_substitute(str(settings.get("welcome_embed_title") or ""), member)[:256] or None,
            description=_substitute(str(settings.get("join_message") or ""), member)[:4000],
            color=discord.Color(accent) if accent is not None else discord.Color.blurple(),
        )
        embed.set_thumbnail(url=member.display_avatar.url)
        image_url = str(settings.get("welcome_embed_image_url") or "")
        if image_url.startswith("https://"):
            embed.set_image(url=image_url)
        footer = _substitute(str(settings.get("welcome_embed_footer") or ""), member)
        if footer:
            embed.set_footer(text=footer[:2048])
        return embed

    async def build_card(self, member: discord.Member, settings: dict) -> bytes | None:
        """The welcome image, or None if it couldn't be made (the text still goes out)."""
        try:
            avatar = await member.display_avatar.replace(size=256, format="png").read()
        except (discord.HTTPException, ValueError):
            avatar = None
        try:
            return await asyncio.get_running_loop().run_in_executor(
                None, lambda: render_card(avatar, name=member.display_name, server=member.guild.name,
                                          member_number=member.guild.member_count or 0, accent=self._accent(settings, member.guild.id)),
            )
        except Exception:  # noqa: BLE001
            LOGGER.exception("Could not render a welcome card in guild %s", member.guild.id)
            return None

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member) -> None:
        if member.bot:
            return
        settings = get_guild_settings(self.bot.storage_path, member.guild.id)
        if not enabled_in(settings, "welcome"):
            return

        dm_text = str(settings.get("welcome_dm_message") or "")
        if dm_text:
            try:
                await member.send(_substitute(dm_text, member)[:2000], allowed_mentions=discord.AllowedMentions.none())
            except discord.HTTPException:
                pass  # DMs closed; the channel greeting still goes out

        channel_ids = settings.get("welcome_channel_ids", []) or []
        message = settings.get("join_message") or ""
        style = str(settings.get("welcome_style") or "text")
        if not channel_ids or (not message and style == "text"):
            return
        if style == "embed":
            await self._send_to_configured_channels(member.guild, channel_ids, member.mention, embed=self.build_embed(member, settings))
        elif style == "image":
            card = await self.build_card(member, settings)
            await self._send_to_configured_channels(member.guild, channel_ids, _substitute(message, member) or member.mention, image=card)
        else:
            await self._send_to_configured_channels(member.guild, channel_ids, _substitute(message, member))

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member) -> None:
        if member.bot:
            return
        settings = get_guild_settings(self.bot.storage_path, member.guild.id)
        if not enabled_in(settings, "welcome"):
            return
        # Leave messages go to their own channels when configured, otherwise
        # they share the welcome channel(s) as before.
        channel_ids = settings.get("leave_channel_ids") or settings.get("welcome_channel_ids") or []
        message = settings.get("leave_message") or ""
        if not channel_ids or not message:
            return
        await self._send_to_configured_channels(member.guild, channel_ids, _substitute(message, member))


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Welcome(bot))
