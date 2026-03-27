from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands


class Moderation(commands.Cog):
    """Moderation commands and listeners."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @app_commands.command(name="say", description="Send an embed containing your message.")
    @app_commands.checks.has_permissions(manage_messages=True)
    @app_commands.guild_only()
    async def say(self, interaction: discord.Interaction, message: str) -> None:
        embed = self.bot.embeds.create(
            title="Message",
            description=message,
            author_name=str(interaction.user),
            author_icon_url=interaction.user.display_avatar.url,
        )
        await self.bot.embeds.respond(interaction, embed=embed)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Moderation(bot))
