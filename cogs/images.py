from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands


class Images(commands.Cog):
    """Image manipulation and meme commands."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @app_commands.command(name="imageinfo", description="Show the current image cog status.")
    async def image_info(self, interaction: discord.Interaction) -> None:
        await self.bot.embeds.info_interaction(
            interaction,
            "Images Cog",
            "Image cog is loaded. Legacy image commands can be migrated here.",
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Images(bot))
