from __future__ import annotations

from datetime import timedelta

import discord
from discord import app_commands
from discord.ext import commands

from utils.settings import command_is_blocked, is_bot_channel


class Moderation(commands.Cog):
    """Moderation commands and listeners."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    async def _ensure_allowed(
        self,
        interaction: discord.Interaction,
        command_name: str,
    ) -> bool:
        if interaction.guild is None or interaction.channel is None:
            await self.bot.embeds.error_interaction(
                interaction,
                "Server Only",
                "This command can only be used in a server.",
                ephemeral=True,
            )
            return False

        if command_is_blocked(self.bot.storage_path, interaction.guild.id, command_name):
            await self.bot.embeds.error_interaction(
                interaction,
                "Command Blocked",
                f"`/{command_name}` is blocked in this server.",
                ephemeral=True,
            )
            return False

        if not is_bot_channel(self.bot.storage_path, interaction.guild.id, interaction.channel.id):
            await self.bot.embeds.warning_interaction(
                interaction,
                "Wrong Channel",
                "This command can only be used in a configured bot channel.",
                ephemeral=True,
            )
            return False

        return True

    @app_commands.command(name="say", description="Send an embed containing your message.")
    @app_commands.checks.has_permissions(manage_messages=True)
    @app_commands.guild_only()
    async def say(self, interaction: discord.Interaction, message: str) -> None:
        if not await self._ensure_allowed(interaction, "say"):
            return

        embed = self.bot.embeds.create(
            title="Message",
            description=message,
            author_name=str(interaction.user),
            author_icon_url=interaction.user.display_avatar.url,
        )
        await self.bot.embeds.respond(interaction, embed=embed)

    @app_commands.command(name="purge", description="Delete a number of recent messages.")
    @app_commands.checks.has_permissions(manage_messages=True)
    @app_commands.guild_only()
    async def purge(
        self,
        interaction: discord.Interaction,
        amount: app_commands.Range[int, 1, 100],
    ) -> None:
        if not await self._ensure_allowed(interaction, "purge"):
            return

        channel = interaction.channel
        if not isinstance(channel, discord.TextChannel):
            await self.bot.embeds.error_interaction(
                interaction,
                "Unsupported Channel",
                "This command only works in a regular text channel.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        deleted = await channel.purge(limit=amount)
        await interaction.followup.send(
            embed=self.bot.embeds.success_embed(
                "Purge Complete",
                f"Deleted `{len(deleted)}` message(s).",
            ),
            ephemeral=True,
        )

    @app_commands.command(name="timeout", description="Timeout a member for a number of minutes.")
    @app_commands.checks.has_permissions(moderate_members=True)
    @app_commands.guild_only()
    async def timeout(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        minutes: app_commands.Range[int, 1, 40320],
        reason: str | None = None,
    ) -> None:
        if not await self._ensure_allowed(interaction, "timeout"):
            return

        until = discord.utils.utcnow() + timedelta(minutes=minutes)
        await member.timeout(until, reason=reason)
        await self.bot.embeds.success_interaction(
            interaction,
            "Member Timed Out",
            f"Timed out {member.mention} for `{minutes}` minute(s).",
            ephemeral=True,
        )

    @app_commands.command(name="untimeout", description="Remove a member's timeout.")
    @app_commands.checks.has_permissions(moderate_members=True)
    @app_commands.guild_only()
    async def untimeout(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        reason: str | None = None,
    ) -> None:
        if not await self._ensure_allowed(interaction, "untimeout"):
            return

        await member.timeout(None, reason=reason)
        await self.bot.embeds.success_interaction(
            interaction,
            "Timeout Removed",
            f"Removed the timeout from {member.mention}.",
            ephemeral=True,
        )

    @app_commands.command(name="kick", description="Kick a member from the server.")
    @app_commands.checks.has_permissions(kick_members=True)
    @app_commands.guild_only()
    async def kick(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        reason: str | None = None,
    ) -> None:
        if not await self._ensure_allowed(interaction, "kick"):
            return

        await member.kick(reason=reason)
        await self.bot.embeds.success_interaction(
            interaction,
            "Member Kicked",
            f"Kicked {member.mention}.",
            ephemeral=True,
        )

    @app_commands.command(name="ban", description="Ban a member from the server.")
    @app_commands.checks.has_permissions(ban_members=True)
    @app_commands.guild_only()
    async def ban(
        self,
        interaction: discord.Interaction,
        user: discord.User,
        delete_message_days: app_commands.Range[int, 0, 7] = 0,
        reason: str | None = None,
    ) -> None:
        if not await self._ensure_allowed(interaction, "ban"):
            return

        guild = interaction.guild
        if guild is None:
            await self.bot.embeds.error_interaction(
                interaction,
                "Server Only",
                "This command can only be used in a server.",
                ephemeral=True,
            )
            return

        await guild.ban(user, reason=reason, delete_message_days=delete_message_days)
        await self.bot.embeds.success_interaction(
            interaction,
            "User Banned",
            f"Banned `{user}`.",
            ephemeral=True,
        )

    @app_commands.command(name="unban", description="Unban a user by ID.")
    @app_commands.checks.has_permissions(ban_members=True)
    @app_commands.guild_only()
    async def unban(
        self,
        interaction: discord.Interaction,
        user_id: str,
        reason: str | None = None,
    ) -> None:
        if not await self._ensure_allowed(interaction, "unban"):
            return

        guild = interaction.guild
        if guild is None:
            await self.bot.embeds.error_interaction(
                interaction,
                "Server Only",
                "This command can only be used in a server.",
                ephemeral=True,
            )
            return

        try:
            user = await self.bot.fetch_user(int(user_id))
        except (TypeError, ValueError, discord.NotFound):
            await self.bot.embeds.error_interaction(
                interaction,
                "Invalid User",
                "Provide a valid user ID.",
                ephemeral=True,
            )
            return

        await guild.unban(user, reason=reason)
        await self.bot.embeds.success_interaction(
            interaction,
            "User Unbanned",
            f"Unbanned `{user}`.",
            ephemeral=True,
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Moderation(bot))
