from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands


class Owner(commands.GroupCog, group_name="owner", group_description="Owner-only maintenance commands"):
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
        await self.bot.reload_extension(extension)
        await self.bot.embeds.success_interaction(
            interaction,
            "Reloaded Extension",
            f"Reloaded `{extension}` successfully.",
            ephemeral=True,
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Owner(bot))
