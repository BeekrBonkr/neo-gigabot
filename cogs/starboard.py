from __future__ import annotations

import logging

import discord
from discord.ext import commands

from utils.settings import (
    get_starboard_channel_id,
    get_starboard_emoji,
    get_starboard_threshold,
    starboard_ignores_nsfw,
    starboard_selfstar_allowed,
)
from utils.starboard import delete_starboard_post, get_starboard_post, save_starboard_post

LOGGER = logging.getLogger(__name__)

MAX_QUOTED_CONTENT = 1000


class Starboard(commands.Cog):
    """Reposts community-starred messages into a configured starboard channel
    once they collect enough ⭐ reactions. Configure the channel and threshold
    from /settings → Starboard; no channel configured means the feature is off."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent) -> None:
        await self._handle_star_change(payload)

    @commands.Cog.listener()
    async def on_raw_reaction_remove(self, payload: discord.RawReactionActionEvent) -> None:
        await self._handle_star_change(payload)

    @commands.Cog.listener()
    async def on_raw_message_delete(self, payload: discord.RawMessageDeleteEvent) -> None:
        # Keep the table from accumulating rows for messages that no longer exist.
        # The starboard post itself is left up - the moment was still starred.
        if payload.guild_id is not None:
            delete_starboard_post(self.bot.storage_path, payload.guild_id, payload.message_id)

    async def _handle_star_change(self, payload: discord.RawReactionActionEvent) -> None:
        if payload.guild_id is None:
            return
        star_emoji = get_starboard_emoji(self.bot.storage_path, payload.guild_id)
        if str(payload.emoji) != star_emoji:
            return

        starboard_channel_id = get_starboard_channel_id(self.bot.storage_path, payload.guild_id)
        if starboard_channel_id is None or payload.channel_id == starboard_channel_id:
            # Off, or someone starring a starboard post itself - don't recurse.
            return

        guild = self.bot.get_guild(payload.guild_id)
        if guild is None:
            return
        starboard_channel = guild.get_channel(starboard_channel_id)
        if not isinstance(starboard_channel, discord.TextChannel):
            return
        source_channel = guild.get_channel(payload.channel_id) or guild.get_thread(payload.channel_id)
        if source_channel is None or not hasattr(source_channel, "fetch_message"):
            return
        if starboard_ignores_nsfw(self.bot.storage_path, guild.id):
            is_nsfw = getattr(source_channel, "is_nsfw", None)
            if callable(is_nsfw) and is_nsfw():
                return

        try:
            message = await source_channel.fetch_message(payload.message_id)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            return

        reaction = next((r for r in message.reactions if str(r.emoji) == star_emoji), None)
        star_count = reaction.count if reaction is not None else 0
        if (
            reaction is not None
            and star_count > 0
            and not starboard_selfstar_allowed(self.bot.storage_path, guild.id)
            and await self._author_reacted(reaction, message.author.id)
        ):
            star_count -= 1
        existing = get_starboard_post(self.bot.storage_path, guild.id, message.id)

        if existing is not None:
            await self._update_existing_post(guild, starboard_channel, message, existing, star_count, star_emoji)
            return

        if star_count < get_starboard_threshold(self.bot.storage_path, guild.id):
            return

        try:
            starboard_message = await starboard_channel.send(
                content=f"{star_emoji} **{star_count}** {source_channel.mention}",
                embed=self._build_star_embed(message),
            )
        except (discord.Forbidden, discord.HTTPException) as exc:
            LOGGER.warning("Could not post to starboard channel %s in guild %s: %s", starboard_channel_id, guild.id, exc)
            return

        save_starboard_post(self.bot.storage_path, guild.id, message.id, starboard_message.id, star_count)

    async def _author_reacted(self, reaction: discord.Reaction, author_id: int) -> bool:
        """Whether the message author is among a reaction's reactors. Costs a
        users() fetch, so only called when self-stars are disallowed."""
        try:
            async for user in reaction.users():
                if user.id == author_id:
                    return True
        except discord.HTTPException:
            pass
        return False

    async def _update_existing_post(
        self,
        guild: discord.Guild,
        starboard_channel: discord.TextChannel,
        message: discord.Message,
        existing: dict,
        star_count: int,
        star_emoji: str,
    ) -> None:
        if star_count == existing["star_count"]:
            return

        try:
            starboard_message = await starboard_channel.fetch_message(existing["starboard_message_id"])
            await starboard_message.edit(content=f"{star_emoji} **{star_count}** {message.channel.mention}")
        except discord.NotFound:
            # The starboard post was deleted (probably by a mod) - treat that
            # as "don't showcase this one", not as a request to repost it.
            delete_starboard_post(self.bot.storage_path, guild.id, message.id)
            return
        except (discord.Forbidden, discord.HTTPException):
            return

        save_starboard_post(self.bot.storage_path, guild.id, message.id, starboard_message.id, star_count)

    def _build_star_embed(self, message: discord.Message) -> discord.Embed:
        content = message.content or ""
        if len(content) > MAX_QUOTED_CONTENT:
            content = content[: MAX_QUOTED_CONTENT - 1] + "…"

        embed = self.bot.embeds.create(
            description=content or "*(no text content)*",
            color=discord.Color.gold(),
            author_name=str(message.author),
            author_icon_url=message.author.display_avatar.url,
            fields=[self.bot.embeds.field("Source", f"[Jump to message]({message.jump_url})", False)],
            timestamp=False,
        )
        embed.timestamp = message.created_at

        image_attachment = next(
            (a for a in message.attachments if (a.content_type or "").startswith("image/")),
            None,
        )
        if image_attachment is not None:
            embed.set_image(url=image_attachment.url)
        elif message.attachments:
            embed.add_field(name="Attachments", value=str(len(message.attachments)), inline=True)

        return embed


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Starboard(bot))
