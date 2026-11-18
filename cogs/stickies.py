from __future__ import annotations

import asyncio
import logging

import discord
from discord import app_commands
from discord.ext import commands

from utils import stickies as store
from utils.modules import module_enabled

LOGGER = logging.getLogger(__name__)
NO_MENTIONS = discord.AllowedMentions.none()


class Stickies(commands.Cog):
    """Sticky messages: /stick keeps a message at the bottom of a channel. As
    people talk, the bot deletes its last copy and posts a new one, after 15
    seconds or 5 messages (35 seconds or 13 messages for a slow sticky)."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.pacer = store.Pacer()
        self._locks: dict[int, asyncio.Lock] = {}

    async def post(self, channel: discord.abc.Messageable, sticky: dict) -> None:
        """Delete the previous copy and send a fresh one."""
        lock = self._locks.setdefault(channel.id, asyncio.Lock())
        async with lock:
            if sticky.get("last_message_id"):
                try:
                    await channel.get_partial_message(sticky["last_message_id"]).delete()
                except discord.HTTPException:
                    pass  # already gone
            try:
                if sticky["embed"]:
                    message = await channel.send(embed=self.bot.embeds.info_embed("Stickied Message", sticky["content"]), allowed_mentions=NO_MENTIONS)
                else:
                    message = await channel.send(store.HEADER + sticky["content"], allowed_mentions=NO_MENTIONS)
            except discord.HTTPException as exc:
                LOGGER.warning("Could not post the sticky in channel %s: %s", channel.id, exc)
                store.set_last_message(self.bot.storage_path, channel.id, None)
                return
            store.set_last_message(self.bot.storage_path, channel.id, message.id)
            self.pacer.posted(channel.id)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.guild is None or message.channel.id not in store.active_channels(self.bot.storage_path):
            return
        if self.bot.user is not None and message.author.id == self.bot.user.id:
            return  # the sticky itself, and the bot's own replies, don't count
        if not module_enabled(self.bot.storage_path, message.guild.id, "stickies"):
            return
        try:
            sticky = store.get(self.bot.storage_path, message.channel.id)
            if sticky and sticky["active"] and self.pacer.should_post(message.channel.id, bool(sticky["slow"])):
                await self.post(message.channel, sticky)
        except Exception:  # noqa: BLE001
            LOGGER.exception("Sticky handling failed in channel %s", message.channel.id)

    async def _stick(self, interaction: discord.Interaction, message: str, *, embed: bool, slow: bool) -> None:
        try:
            sticky = store.set_sticky(self.bot.storage_path, interaction.guild.id, interaction.channel.id, message, interaction.user.id, embed=embed, slow=slow)
        except store.StickyError as exc:
            await self.bot.embeds.error_interaction(interaction, "Not Stuck", str(exc), ephemeral=True)
            return
        await interaction.response.send_message("✅ Stuck. Use `/stickstop` to pause it or `/stickremove` to delete it.", ephemeral=True)
        await self.post(interaction.channel, sticky)

    @app_commands.command(name="stick", description="Stick a message to the bottom of this channel (or replace the one there).")
    @app_commands.describe(message="The text to keep at the bottom. Type \\n for a new line")
    @app_commands.checks.has_permissions(manage_messages=True)
    @app_commands.guild_only()
    async def stick(self, interaction: discord.Interaction, message: app_commands.Range[str, 1, store.MAX_CONTENT_LENGTH]) -> None:
        await self._stick(interaction, message, embed=False, slow=False)

    @app_commands.command(name="stickembed", description="Stick a message to this channel as an embed.")
    @app_commands.describe(message="The text to keep at the bottom. Type \\n for a new line")
    @app_commands.checks.has_permissions(manage_messages=True)
    @app_commands.checks.bot_has_permissions(embed_links=True)
    @app_commands.guild_only()
    async def stickembed(self, interaction: discord.Interaction, message: app_commands.Range[str, 1, store.MAX_CONTENT_LENGTH]) -> None:
        await self._stick(interaction, message, embed=True, slow=False)

    @app_commands.command(name="stickslow", description="Stick a message that is re-posted less often, for busy channels.")
    @app_commands.describe(message="The text to keep at the bottom. Type \\n for a new line")
    @app_commands.checks.has_permissions(manage_messages=True)
    @app_commands.guild_only()
    async def stickslow(self, interaction: discord.Interaction, message: app_commands.Range[str, 1, store.MAX_CONTENT_LENGTH]) -> None:
        await self._stick(interaction, message, embed=False, slow=True)

    @app_commands.command(name="stickstop", description="Stop the sticky in this channel. /stickstart brings it back.")
    @app_commands.checks.has_permissions(manage_messages=True)
    @app_commands.guild_only()
    async def stickstop(self, interaction: discord.Interaction) -> None:
        sticky = store.set_active(self.bot.storage_path, interaction.channel.id, False)
        if sticky is None:
            await self.bot.embeds.error_interaction(interaction, "No Sticky", "There is no sticky in this channel.", ephemeral=True)
            return
        await interaction.response.send_message("✅ Stopped. The message is kept; `/stickstart` restarts it.", ephemeral=True)

    @app_commands.command(name="stickstart", description="Restart this channel's stopped sticky with its previous message.")
    @app_commands.checks.has_permissions(manage_messages=True)
    @app_commands.guild_only()
    async def stickstart(self, interaction: discord.Interaction) -> None:
        sticky = store.set_active(self.bot.storage_path, interaction.channel.id, True)
        if sticky is None:
            await self.bot.embeds.error_interaction(interaction, "No Sticky", "There is no sticky in this channel. Create one with `/stick`.", ephemeral=True)
            return
        await interaction.response.send_message("✅ Restarted.", ephemeral=True)
        await self.post(interaction.channel, sticky)

    @app_commands.command(name="stickremove", description="Stop and delete the sticky in this channel.")
    @app_commands.checks.has_permissions(manage_messages=True)
    @app_commands.guild_only()
    async def stickremove(self, interaction: discord.Interaction) -> None:
        sticky = store.remove(self.bot.storage_path, interaction.channel.id)
        if sticky is None:
            await self.bot.embeds.error_interaction(interaction, "No Sticky", "There is no sticky in this channel.", ephemeral=True)
            return
        self.pacer.forget(interaction.channel.id)
        if sticky.get("last_message_id"):
            try:
                await interaction.channel.get_partial_message(sticky["last_message_id"]).delete()
            except discord.HTTPException:
                pass
        await interaction.response.send_message("✅ Removed.", ephemeral=True)

    @app_commands.command(name="getstickies", description="List the stickies in this server.")
    @app_commands.checks.has_permissions(manage_messages=True)
    @app_commands.guild_only()
    async def getstickies(self, interaction: discord.Interaction) -> None:
        stickies = store.list_for_guild(self.bot.storage_path, interaction.guild.id)
        lines = [
            f"<#{s['channel_id']}> · {'active' if s['active'] else 'stopped'}{', slow' if s['slow'] else ''}{', embed' if s['embed'] else ''}\n> {s['content'][:120]}"
            for s in stickies
        ] or ["No stickies. Create one with `/stick` in the channel it belongs to."]
        await self.bot.embeds.respond(interaction, embed=self.bot.embeds.info_embed(f"Stickies ({len(stickies)})", "\n".join(lines)[:4000]), ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Stickies(bot))
