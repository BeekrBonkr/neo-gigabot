from __future__ import annotations

import asyncio
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands

from utils.settings import get_guild_settings, reset_guild_settings, sync_all_guild_settings


class Owner(
    commands.GroupCog,
    group_name="owner",
    group_description="Owner-only maintenance commands",
):
    """Owner-only diagnostics and maintenance commands."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        super().__init__()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.bot.config.owner_id:
            await self.bot.embeds.error_interaction(
                interaction,
                "Owner Only",
                "Only the configured owner can use this command.",
                ephemeral=True,
            )
            return False
        return True

    @app_commands.command(name="reload", description="Reload a cog extension.")
    async def reload_extension(self, interaction: discord.Interaction, extension: str) -> None:
        normalized = extension if extension.startswith("cogs.") else f"cogs.{extension}"
        await self.bot.reload_extension(normalized)
        await self.bot.embeds.success_interaction(
            interaction,
            "Reloaded Extension",
            f"Reloaded `{normalized}` successfully.",
            ephemeral=True,
        )

    @app_commands.command(name="reloadall", description="Reload all configured cogs.")
    async def reload_all(self, interaction: discord.Interaction) -> None:
        succeeded: list[str] = []
        failed: list[str] = []
        for extension in list(self.bot.extensions):
            try:
                await self.bot.reload_extension(extension)
                succeeded.append(extension)
            except Exception as exc:
                failed.append(f"`{extension}`: `{type(exc).__name__}: {exc}`")

        fields = [
            self.bot.embeds.field(
                "Reloaded",
                "\n".join(f"- `{name}`" for name in succeeded) or "None",
                False,
            ),
            self.bot.embeds.field(
                "Failed",
                "\n".join(failed) or "None",
                False,
            ),
        ]
        await self.bot.embeds.respond(
            interaction,
            title="Reload All Complete",
            fields=fields,
            ephemeral=True,
        )

    @app_commands.command(name="sync", description="Sync application commands.")
    async def sync(self, interaction: discord.Interaction, global_sync: bool = False) -> None:
        if global_sync:
            synced = await self.bot.tree.sync()
            scope = "globally"
        elif interaction.guild is not None:
            synced = await self.bot.tree.sync(guild=interaction.guild)
            scope = f"to `{interaction.guild.name}`"
        else:
            synced = await self.bot.tree.sync()
            scope = "globally"

        await self.bot.embeds.success_interaction(
            interaction,
            "Command Sync Complete",
            f"Synced `{len(synced)}` command(s) {scope}.",
            ephemeral=True,
        )

    @app_commands.command(name="guildsettings", description="Inspect the raw settings for a guild ID.")
    async def guild_settings(self, interaction: discord.Interaction, guild_id: str) -> None:
        try:
            parsed_guild_id = int(guild_id)
        except ValueError:
            await self.bot.embeds.error_interaction(
                interaction,
                "Invalid Guild ID",
                "Provide a numeric guild ID.",
                ephemeral=True,
            )
            return

        settings = get_guild_settings(self.bot.storage_path, parsed_guild_id)
        lines = [f"`{key}`: `{value}`" for key, value in settings.items()]
        await self.bot.embeds.respond(
            interaction,
            title="Guild Settings",
            description="\n".join(lines[:25]) or "No settings found.",
            footer=f"Guild ID: {parsed_guild_id}",
            ephemeral=True,
        )

    @app_commands.command(name="resyncguild", description="Reset one guild to the current default settings schema.")
    async def resync_guild(self, interaction: discord.Interaction, guild_id: str) -> None:
        try:
            parsed_guild_id = int(guild_id)
        except ValueError:
            await self.bot.embeds.error_interaction(
                interaction,
                "Invalid Guild ID",
                "Provide a numeric guild ID.",
                ephemeral=True,
            )
            return

        reset_guild_settings(self.bot.storage_path, parsed_guild_id)
        await self.bot.embeds.success_interaction(
            interaction,
            "Guild Settings Reset",
            f"Reset settings for guild `{parsed_guild_id}` to the current defaults.",
            ephemeral=True,
        )

    @app_commands.command(name="storage", description="Show storage paths and resync settings schema.")
    async def storage(self, interaction: discord.Interaction) -> None:
        updated = sync_all_guild_settings(self.bot.storage_path)
        fields = [
            self.bot.embeds.field("Project Root", str(Path(self.bot.project_root)), False),
            self.bot.embeds.field("Storage Path", str(Path(self.bot.storage_path)), False),
            self.bot.embeds.field("Updated Guild Records", str(updated), True),
        ]
        await self.bot.embeds.respond(
            interaction,
            title="Storage Status",
            fields=fields,
            ephemeral=True,
        )

    @app_commands.command(name="extensions", description="List loaded extensions.")
    async def extensions(self, interaction: discord.Interaction) -> None:
        loaded = sorted(self.bot.extensions)
        await self.bot.embeds.respond(
            interaction,
            title="Loaded Extensions",
            description="\n".join(f"- `{name}`" for name in loaded) or "No extensions loaded.",
            ephemeral=True,
        )

    @app_commands.command(name="guilds", description="List guilds the bot is currently in.")
    async def guilds(self, interaction: discord.Interaction) -> None:
        rows = [f"- `{guild.id}` • **{guild.name}** • `{guild.member_count or 0}` members" for guild in sorted(self.bot.guilds, key=lambda g: g.name.lower())]
        await self.bot.embeds.respond(
            interaction,
            title="Connected Guilds",
            description="\n".join(rows[:50]) or "The bot is not in any guilds.",
            footer=f"Showing {min(len(rows), 50)} of {len(rows)} guild(s)",
            ephemeral=True,
        )

    @app_commands.command(name="health", description="Show simple bot health diagnostics.")
    async def health(self, interaction: discord.Interaction) -> None:
        latency_ms = round(self.bot.latency * 1000)
        fields = [
            self.bot.embeds.field("Guilds", str(len(self.bot.guilds)), True),
            self.bot.embeds.field("Users Cached", str(len(self.bot.users)), True),
            self.bot.embeds.field("Latency", f"`{latency_ms} ms`", True),
            self.bot.embeds.field("Loaded Extensions", str(len(self.bot.extensions)), True),
        ]
        await self.bot.embeds.respond(
            interaction,
            title="Bot Health",
            fields=fields,
            ephemeral=True,
        )

    @app_commands.command(name="shutdown", description="Gracefully shut the bot down.")
    async def shutdown(self, interaction: discord.Interaction) -> None:
        await self.bot.embeds.warning_interaction(
            interaction,
            "Shutting Down",
            "Closing the bot connection now.",
            ephemeral=True,
        )
        asyncio.create_task(self.bot.close())


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Owner(bot))
